"""FastAPI application and same-origin frontend hosting."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from leanharness.application.session_gateway import (
    apply_first_task_title,
    ensure_session,
    persist_model_event,
    persist_runtime_event,
    persist_stream_cancellation,
    session_detail,
    session_to_dict,
)
from leanharness.config import AppConfig
from leanharness.errors import (
    ChatInputError,
    InvalidPermissionError,
    LeanHarnessError,
    ModelNotConfiguredError,
    RunInputError,
    SessionNotFoundError,
    StorageError,
)
from leanharness.models import (
    ModelEvent,
    OpenAICompatibleClient,
    get_model_config_status,
    load_model_config,
)
from leanharness.runtime import RuntimeEvent
from leanharness.storage import LocalStore


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class RunRequest(BaseModel):
    task: str
    max_steps: int = 24
    session_id: str | None = None


class SessionCreateRequest(BaseModel):
    title: str | None = None
    permission_mode: str = "inspect"


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    permission_mode: str | None = None


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

    store = LocalStore(config.data_dir)
    store.open()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="LeanHarness API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.frontend_dir = (frontend_dir or default_frontend_dir()).resolve()
    app.state.model_client_factory = model_client_factory
    app.state.store = store

    @app.exception_handler(LeanHarnessError)
    async def application_error(_request: Request, exc: LeanHarnessError) -> JSONResponse:
        status_code = 503 if isinstance(exc, ModelNotConfiguredError) else 502
        if isinstance(exc, ChatInputError | RunInputError):
            status_code = 422
        elif isinstance(exc, SessionNotFoundError):
            status_code = 404
        elif isinstance(exc, InvalidPermissionError):
            status_code = 422
        elif isinstance(exc, StorageError):
            status_code = 500
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

    @app.get("/api/v1/sessions")
    async def sessions() -> dict[str, object]:
        store: LocalStore = app.state.store
        project = store.ensure_project(config.workspace)
        return {"sessions": [session_to_dict(session) for session in store.list_sessions(project)]}

    @app.post("/api/v1/sessions")
    async def create_session(payload: SessionCreateRequest) -> dict[str, object]:
        store: LocalStore = app.state.store
        project = store.ensure_project(config.workspace, permission_mode=payload.permission_mode)
        session = store.create_session(
            project,
            title=payload.title or "新会话",
            permission_mode=payload.permission_mode,
        )
        return session_to_dict(session)

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        ensure_session(app.state.store, config.workspace, session_id)
        return session_detail(app.state.store, session_id)

    @app.patch("/api/v1/sessions/{session_id}")
    async def update_session(session_id: str, payload: SessionUpdateRequest) -> dict[str, object]:
        ensure_session(app.state.store, config.workspace, session_id)
        session = app.state.store.update_session(
            session_id,
            title=payload.title,
            permission_mode=payload.permission_mode,
        )
        return session_to_dict(session)

    @app.delete("/api/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, object]:
        ensure_session(app.state.store, config.workspace, session_id)
        app.state.store.delete_session(session_id)
        return {"deleted": True, "session_id": session_id}

    @app.post("/api/v1/model/check")
    async def model_check() -> dict[str, object]:
        result = await check_model(client_factory=app.state.model_client_factory)
        return result.to_dict()

    @app.post("/api/v1/chat")
    async def chat(payload: ChatRequest) -> StreamingResponse:
        message = validate_chat_message(payload.message)
        model_config = load_model_config()
        store: LocalStore = app.state.store
        _, session = ensure_session(store, config.workspace, payload.session_id)
        session = apply_first_task_title(store, session, message)
        run = store.create_run(session.id, "chat", message, 1)
        store.add_message(session.id, "user", message, run_id=run.id)

        async def ndjson_events() -> AsyncIterator[bytes]:
            content: list[str] = []
            last_sequence = -1
            terminal_seen = False
            try:
                async for event in stream_chat(
                    message,
                    config=model_config,
                    client_factory=app.state.model_client_factory,
                    language=session.language or "same",
                ):
                    last_sequence = event.sequence
                    persist_model_event(store, session, run, event)
                    if event.type == "content.delta" and event.content:
                        content.append(event.content)
                    if event.type == "turn.completed":
                        if content:
                            store.add_message(
                                session.id,
                                "assistant",
                                "".join(content),
                                "complete",
                                run_id=run.id,
                            )
                        store.update_run(run.id, state="COMPLETED", answer="".join(content))
                    terminal_seen = event.type in {"turn.completed", "turn.failed"}
                    yield _encode_event(event, session_id=session.id, run_id=run.id)
            except (asyncio.CancelledError, GeneratorExit):
                if not terminal_seen:
                    persist_stream_cancellation(
                        store,
                        session,
                        run,
                        sequence=last_sequence + 1,
                        mode="chat",
                        partial_answer="".join(content) or None,
                    )
                raise

        return StreamingResponse(
            ndjson_events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/v1/runs")
    async def run(payload: RunRequest) -> StreamingResponse:
        store: LocalStore = app.state.store
        _, session = ensure_session(store, config.workspace, payload.session_id)
        session = apply_first_task_title(store, session, payload.task)
        run_record = store.create_run(session.id, "inspect", payload.task, payload.max_steps)
        runtime = create_inspection_run(
            payload.task,
            config.workspace,
            max_steps=payload.max_steps,
            client_factory=app.state.model_client_factory,
            run_id=run_record.id,
            language=session.language or "same",
        )
        store.add_message(session.id, "user", payload.task, run_id=run_record.id)

        async def ndjson_events() -> AsyncIterator[bytes]:
            last_sequence = -1
            terminal_seen = False
            try:
                async for event in runtime.run(payload.task):
                    last_sequence = event.sequence
                    persist_runtime_event(store, session, run_record, event)
                    terminal_seen = event.type in {
                        "run.completed",
                        "run.incomplete",
                        "run.failed",
                        "run.cancelled",
                    }
                    yield _encode_payload(
                        event.to_dict(), session_id=session.id, run_id=run_record.id
                    )
            except (asyncio.CancelledError, GeneratorExit):
                if not terminal_seen:
                    cancellation = RuntimeEvent(
                        type="run.cancelled",
                        sequence=last_sequence + 1,
                        run_id=run_record.id,
                        summary="Run cancelled",
                    )
                    persist_runtime_event(store, session, run_record, cancellation)
                raise

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


def _encode_event(
    event: ModelEvent, *, session_id: str | None = None, run_id: str | None = None
) -> bytes:
    return _encode_payload(event.to_dict(), session_id=session_id, run_id=run_id)


def _encode_payload(
    payload: dict[str, object], *, session_id: str | None = None, run_id: str | None = None
) -> bytes:
    if session_id is not None:
        payload = {**payload, "session_id": session_id}
    if run_id is not None:
        payload = {**payload, "run_id": run_id}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
