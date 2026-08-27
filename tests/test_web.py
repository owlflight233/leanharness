import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI

from leanharness.config import build_config
from leanharness.web.app import create_app


def get(app: FastAPI, path: str) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(request())


def test_health_contract_is_exact(tmp_path: Path) -> None:
    config = build_config(workspace=tmp_path)
    app = create_app(config, frontend_dir=tmp_path / "missing-frontend")

    response = get(app, "/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "name": "LeanHarness",
        "version": "0.1.0.dev0",
        "workspace": str(tmp_path.resolve()),
        "capabilities": [],
    }


def test_frontend_build_is_served_with_spa_fallback(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>LeanHarness</main>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ready')", encoding="utf-8")
    config = build_config(workspace=tmp_path)
    app = create_app(config, frontend_dir=frontend)

    assert get(app, "/").text == "<main>LeanHarness</main>"
    assert get(app, "/sessions/example").text == "<main>LeanHarness</main>"
    assert get(app, "/assets/app.js").text == "console.log('ready')"


def test_missing_frontend_has_actionable_response(tmp_path: Path) -> None:
    config = build_config(workspace=tmp_path)
    app = create_app(config, frontend_dir=tmp_path / "missing")

    response = get(app, "/")

    assert response.status_code == 503
    assert response.json()["status"] == "frontend-unavailable"


def test_unknown_api_route_does_not_fall_back_to_html(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>LeanHarness</main>", encoding="utf-8")
    config = build_config(workspace=tmp_path)
    app = create_app(config, frontend_dir=frontend)

    response = get(app, "/api/v1/unknown")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
