import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from leanharness.config import build_config
from leanharness.models import ModelEvent, ModelResponse, ModelUsage, ToolCall
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


class PlanModelClient:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="检查项目结构",
                tool_calls=(
                    ToolCall(
                        id="inspect-1",
                        name="workspace_list",
                        arguments={"path": "."},
                    ),
                ),
            )
        return ModelResponse(content="# Demo plan\n1. **Inspect** - Read the project")


class HistoryRuntimeClient:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) % 2 == 1:
            return ModelResponse(
                content="Inspecting the workspace.",
                tool_calls=(
                    ToolCall(
                        id=f"list-{len(self.requests)}",
                        name="workspace_list",
                        arguments={"path": "."},
                    ),
                ),
            )
        return ModelResponse(content="The workspace was inspected.")


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
            "agent.edit",
            "tool.mkdir",
            "tool.patch",
            "tool.command",
            "tool.git.read",
            "approval.interactive",
        ],
    }


def test_workspace_can_be_selected_for_subsequent_requests(tmp_path: Path) -> None:
    next_workspace = tmp_path / "next"
    next_workspace.mkdir()
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    selected = post(app, "/api/v1/workspace", json_body={"path": str(next_workspace)})

    assert selected.status_code == 200
    assert selected.json() == {"workspace": str(next_workspace.resolve())}
    assert get(app, "/api/v1/health").json()["workspace"] == str(next_workspace.resolve())
    assert get(app, "/api/v1/sessions").json()["sessions"] == []


def test_workspace_can_be_created_and_becomes_current(tmp_path: Path) -> None:
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))
    new_workspace = tmp_path / "new-project"

    created = post(app, "/api/v1/workspace/create", json_body={"path": str(new_workspace)})

    assert created.status_code == 200
    assert created.json() == {"workspace": str(new_workspace.resolve()), "created": True}
    assert new_workspace.is_dir()
    assert get(app, "/api/v1/health").json()["workspace"] == str(new_workspace.resolve())


def test_projects_list_groups_known_workspaces(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    app = create_app(build_config(workspace=first, data_dir=tmp_path / "data"))

    post(app, "/api/v1/sessions", json_body={})
    post(app, "/api/v1/workspace", json_body={"path": str(second)})
    post(app, "/api/v1/sessions", json_body={})
    response = get(app, "/api/v1/projects")

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_workspace"] == str(second.resolve())
    assert {item["root_path"] for item in payload["projects"]} == {
        str(first.resolve()),
        str(second.resolve()),
    }


def test_workspace_create_rejects_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    response = post(app, "/api/v1/workspace/create", json_body={"path": str(target)})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "INVALID_WORKSPACE"


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


def test_coding_runs_replay_bounded_public_history_with_same_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example-model")
    client = HistoryRuntimeClient()
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _config: client,
    )

    first = post(app, "/api/v1/runs", json_body={"task": "Inspect the workspace"})
    first_events = [json.loads(line) for line in first.text.splitlines()]
    session_id = first_events[0]["session_id"]
    second = post(
        app,
        "/api/v1/runs",
        json_body={"task": "What did we do before?", "session_id": session_id},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    second_request = client.requests[2]
    contents = [message.content for message in second_request.messages]
    assert "Inspect the workspace" in contents
    assert "The workspace was inspected." in contents
    assert "What did we do before?" in contents


@pytest.mark.parametrize("body", [{}, {"message": "   "}, {"message": "x" * 32_001}])
def test_chat_rejects_invalid_input_before_streaming(
    tmp_path: Path, body: dict[str, object]
) -> None:
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    response = post(app, "/api/v1/chat", json_body=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CHAT_INPUT"


def test_plan_stream_emits_research_events_and_persists_plan_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example-model")
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _config: PlanModelClient(),
    )

    response = post(app, "/api/v1/plans/stream", json_body={"task": "Inspect the project"})
    events = [json.loads(line) for line in response.text.splitlines()]

    assert response.status_code == 200
    assert any(event["type"] == "tool.completed" for event in events)
    created = next(event for event in events if event["type"] == "plan.created")
    assert created["plan"]["title"] == "Demo plan"
    session_id = created["session_id"]
    detail = get(app, f"/api/v1/sessions/{session_id}").json()
    assert any(message.get("kind") == "plan" for message in detail["messages"])
    audit_events = [event for run in detail["runs"] for event in run.get("trace", [])]
    created_audit = next(event for event in audit_events if event["type"] == "plan.created")
    assert created_audit["step_count"] == 1
    assert "source_markdown" not in json.dumps(created_audit, ensure_ascii=False)
