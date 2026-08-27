"""FastAPI application and same-origin frontend hosting."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from leanharness import __version__
from leanharness.application.health import get_health
from leanharness.config import AppConfig


def default_frontend_dir() -> Path:
    """Locate the repository frontend build when running from a source checkout."""

    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(config: AppConfig, *, frontend_dir: Path | None = None) -> FastAPI:
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

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        return get_health(config).to_dict()

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
