"""FastAPI application and same-origin frontend hosting."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from leanharness import __version__
from leanharness.application.agent_gateway import create_inspection_run
from leanharness.application.health import get_health
from leanharness.application.model_gateway import (
    ModelClientFactory,
    check_model,
    stream_chat,
    validate_chat_message,
)
from leanharness.config import AppConfig
from leanharness.errors import (
    ChatInputError,
    LeanHarnessError,
    ModelNotConfiguredError,
    RunInputError,
)
from leanharness.models import (
    ModelEvent,
    OpenAICompatibleClient,
    get_model_config_status,
    load_model_config,
)


class ChatRequest(BaseModel):
    message: str


class RunRequest(BaseModel):
    task: str
    max_steps: int = 24


def default_frontend_dir() -> Path:
    """Locate the repository frontend build when running from a source checkout."""

    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(
    config: AppConfig,
    *,
    frontend_dir: Path | None = None,
    model_client_factory: ModelClientFactory = OpenAICompatibleClient,
) -> FastAPI:
    """Create the local API without broad cross-origin access."""

    app = FastAPI(
        title="LeanHarness API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.config = config
    app.state.frontend_dir = (frontend_dir or default_frontend_dir()).resolve()
    app.state.model_client_factory = model_client_factory

    @app.exception_handler(LeanHarnessError)
    async def application_error(_request: Request, exc: LeanHarnessError) -> JSONResponse:
        status_code = 503 if isinstance(exc, ModelNotConfiguredError) else 502
        if isinstance(exc, ChatInputError | RunInputError):
            status_code = 422
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
        is_run = request.url.path == "/api/v1/runs"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_RUN_INPUT" if is_run else "INVALID_CHAT_INPUT",
                    "message": (
                        "Request body must contain a task"
                        if is_run
                        else "Request body must contain a text message"
                    ),
                }
            },
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        return get_health(config).to_dict()

    @app.get("/api/v1/model/status")
    async def model_status() -> dict[str, object]:
        status = get_model_config_status()
        return {
            "configured": status.configured,
            "protocol": status.protocol,
            "model": status.model,
        }

    @app.post("/api/v1/model/check")
    async def model_check() -> dict[str, object]:
        result = await check_model(client_factory=app.state.model_client_factory)
        return result.to_dict()

    @app.post("/api/v1/chat")
    async def chat(payload: ChatRequest) -> StreamingResponse:
        message = validate_chat_message(payload.message)
        model_config = load_model_config()

        async def ndjson_events() -> AsyncIterator[bytes]:
            async for event in stream_chat(
                message,
                config=model_config,
                client_factory=app.state.model_client_factory,
            ):
                yield _encode_event(event)

        return StreamingResponse(
            ndjson_events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/v1/runs")
    async def run(payload: RunRequest) -> StreamingResponse:
        runtime = create_inspection_run(
            payload.task,
            config.workspace,
            max_steps=payload.max_steps,
            client_factory=app.state.model_client_factory,
        )

        async def ndjson_events() -> AsyncIterator[bytes]:
            async for event in runtime.run(payload.task):
                yield _encode_payload(event.to_dict())

        return StreamingResponse(
            ndjson_events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/", include_in_schema=False)
    @app.get("/{requested_path:path}", include_in_schema=False)
    async def frontend(requested_path: str = "") -> Response:
        if requested_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")

        root: Path = app.state.frontend_dir
        index = root / "index.html"
        requested = (root / requested_path).resolve()
        if requested.is_relative_to(root) and requested.is_file():
            return FileResponse(requested)
        if index.is_file() and not Path(requested_path).suffix:
            return FileResponse(index)
        if not index.is_file():
            return JSONResponse(
                status_code=503,
                content={
                    "status": "frontend-unavailable",
                    "detail": "Build the React client with `pnpm build` in frontend/.",
                },
            )
        raise HTTPException(status_code=404, detail="Static asset not found")

    return app


def _encode_event(event: ModelEvent) -> bytes:
    return _encode_payload(event.to_dict())


def _encode_payload(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
