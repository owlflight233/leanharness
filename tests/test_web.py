import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from leanharness.config import build_config
from leanharness.models import ModelEvent, ModelResponse, ModelUsage
from leanharness.web.app import create_app


def get(app: FastAPI, path: str) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(request())


def post(app: FastAPI, path: str, *, json_body: dict[str, object] | None = None) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=json_body)

    return asyncio.run(request())


def patch(app: FastAPI, path: str, json_body: dict[str, object]) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.patch(path, json=json_body)

    return asyncio.run(request())


def delete(app: FastAPI, path: str) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.delete(path)

    return asyncio.run(request())


class FakeModelClient:
    async def complete(self, request):
        return ModelResponse(content="ready")

    async def stream(self, request):
        yield ModelEvent(type="turn.started", sequence=0)
        yield ModelEvent(type="content.delta", sequence=1, content="hello 世界")
        yield ModelEvent(
            type="usage.reported",
            sequence=2,
            usage=ModelUsage(prompt_tokens=2, completion_tokens=2, total_tokens=4),
        )
        yield ModelEvent(type="turn.completed", sequence=3, finish_reason="stop")


def test_health_contract_is_exact(tmp_path: Path) -> None:
    config = build_config(workspace=tmp_path, data_dir=tmp_path / "data")
    app = create_app(config, frontend_dir=tmp_path / "missing-frontend")

    response = get(app, "/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "name": "LeanHarness",
        "version": "0.1.0.dev0",
        "workspace": str(tmp_path.resolve()),
        "capabilities": [
            "model.chat",
            "model.streaming",
            "agent.inspect",
            "agent.streaming",
            "session.persistence",
            "run.trace",
        ],
    }


def test_frontend_build_is_served_with_spa_fallback(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>LeanHarness</main>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ready')", encoding="utf-8")
    config = build_config(workspace=tmp_path, data_dir=tmp_path / "data")
    app = create_app(config, frontend_dir=frontend)

    assert get(app, "/").text == "<main>LeanHarness</main>"
    assert get(app, "/sessions/example").text == "<main>LeanHarness</main>"
    assert get(app, "/assets/app.js").text == "console.log('ready')"


def test_missing_frontend_has_actionable_response(tmp_path: Path) -> None:
    config = build_config(workspace=tmp_path, data_dir=tmp_path / "data")
    app = create_app(config, frontend_dir=tmp_path / "missing")

    response = get(app, "/")

    assert response.status_code == 503
    assert response.json()["status"] == "frontend-unavailable"


def test_unknown_api_route_does_not_fall_back_to_html(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>LeanHarness</main>", encoding="utf-8")
    config = build_config(workspace=tmp_path, data_dir=tmp_path / "data")
    app = create_app(config, frontend_dir=frontend)

    response = get(app, "/api/v1/unknown")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_session_crud_contract_and_permissions(tmp_path: Path) -> None:
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    created = post(
        app,
        "/api/v1/sessions",
        json_body={"title": "Repository review", "permission_mode": "approve"},
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert created.json()["permission_mode"] == "approve"
    listed = get(app, "/api/v1/sessions").json()["sessions"]
    assert listed[0]["id"] == session_id

    updated = patch(
        app,
        f"/api/v1/sessions/{session_id}",
        {"title": "Renamed", "permission_mode": "unrestricted"},
    )
    assert updated.json()["title"] == "Renamed"
    assert updated.json()["permission_mode"] == "unrestricted"
    assert get(app, f"/api/v1/sessions/{session_id}").json()["messages"] == []

    removed = delete(app, f"/api/v1/sessions/{session_id}")
    assert removed.json() == {"deleted": True, "session_id": session_id}
    assert get(app, f"/api/v1/sessions/{session_id}").status_code == 404


def test_session_rejects_unknown_permission(tmp_path: Path) -> None:
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))
    response = post(
        app,
        "/api/v1/sessions",
        json_body={"permission_mode": "admin"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_PERMISSION_MODE"


def test_model_status_is_safe_when_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEANHARNESS_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("LEANHARNESS_MODEL_NAME", raising=False)
    monkeypatch.setenv("LEANHARNESS_MODEL_API_KEY", "must-not-leak")
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    response = get(app, "/api/v1/model/status")

    assert response.json() == {
        "configured": False,
        "protocol": "openai-compatible",
        "model": None,
    }
    assert "must-not-leak" not in response.text

    check = post(app, "/api/v1/model/check")
    assert check.status_code == 503
    assert check.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"
    assert "must-not-leak" not in check.text


def test_model_check_and_chat_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example-model")
    monkeypatch.setenv("LEANHARNESS_MODEL_API_KEY", "must-not-leak")
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _config: FakeModelClient(),
    )

    check = post(app, "/api/v1/model/check")
    chat = post(app, "/api/v1/chat", json_body={"message": "hi"})
    events = [json.loads(line) for line in chat.text.splitlines()]

    assert check.status_code == 200
    assert check.json()["status"] == "ok"
    assert check.json()["model"] == "example-model"
    assert "must-not-leak" not in check.text
    assert chat.status_code == 200
    assert chat.headers["content-type"].startswith("application/x-ndjson")
    assert [event["type"] for event in events] == [
        "turn.started",
        "content.delta",
        "usage.reported",
        "turn.completed",
    ]
    assert events[1]["content"] == "hello 世界"


@pytest.mark.parametrize("body", [{}, {"message": "   "}, {"message": "x" * 32_001}])
def test_chat_rejects_invalid_input_before_streaming(
    tmp_path: Path, body: dict[str, object]
) -> None:
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    response = post(app, "/api/v1/chat", json_body=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CHAT_INPUT"
