import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from leanharness.application.model_settings import LocalModelSettings, LocalModelSettingsStore
from leanharness.config import build_config
from leanharness.models import ModelResponse, ToolCall
from leanharness.permissions import PermissionMode
from leanharness.planning import PlanState, PlanStep
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


def post_file(
    app: FastAPI,
    path: str,
    *,
    filename: str,
    media_type: str,
    content: bytes,
) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                files={"file": (filename, content, media_type)},
            )

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


class PlanResumeModelClient:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="Creating the requested directory.",
                tool_calls=(
                    ToolCall(
                        id="mkdir-1",
                        name="workspace_mkdir",
                        arguments={"path": "mini-app"},
                    ),
                ),
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        id="observe-1",
                        name="workspace_list",
                        arguments={"path": "."},
                    ),
                ),
            )
        return ModelResponse(content="The directory was created.")


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


class CapturingRuntimeClient:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) % 2 == 1:
            return ModelResponse(
                content="Inspecting the workspace.",
                tool_calls=(
                    ToolCall("list-workspace", "workspace_list", {"path": "."}),
                ),
            )
        return ModelResponse(
            content="",
            tool_calls=(
                ToolCall(
                    "finish-run",
                    "report_run_outcome",
                    {"status": "completed", "answer": "Inspection complete."},
                ),
            ),
        )


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
            "agent.delegation",
            "session.persistence",
            "run.trace",
            "agent.edit",
            "tool.mkdir",
            "tool.patch",
            "tool.command",
            "tool.git.read",
            "approval.interactive",
            "input.interactive",
            "input.attachment",
            "plugin.local",
            "tool.docx",
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
    assert [item["root_path"] for item in payload["projects"]] == [
        str(first.resolve()),
        str(second.resolve()),
    ]

    for workspace in (first, second, first, second):
        post(app, "/api/v1/workspace", json_body={"path": str(workspace)})
        get(app, "/api/v1/sessions")
        assert [
            item["root_path"] for item in get(app, "/api/v1/projects").json()["projects"]
        ] == [str(first.resolve()), str(second.resolve())]


def test_active_run_permission_cannot_be_changed(tmp_path: Path) -> None:
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))
    session = post(app, "/api/v1/sessions", json_body={}).json()
    app.state.active_runs.acquire(session["id"], "active-run")

    response = patch(
        app,
        f"/api/v1/sessions/{session['id']}",
        json_body={"permission_mode": "unrestricted"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_ALREADY_ACTIVE"
    assert get(app, f"/api/v1/sessions/{session['id']}").json()["session"][
        "permission_mode"
    ] == "inspect"


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


def test_model_status_uses_persistent_non_secret_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.delenv("LEANHARNESS_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("LEANHARNESS_MODEL_NAME", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    LocalModelSettingsStore(data_dir).save(
        LocalModelSettings(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash-vision-exp",
        )
    )
    app = create_app(build_config(workspace=tmp_path, data_dir=data_dir))

    response = get(app, "/api/v1/model/status")

    assert response.json() == {
        "configured": True,
        "protocol": "openai-compatible",
        "model": "deepseek-v4-flash-vision-exp",
    }
    assert "must-not-leak" not in response.text


def test_attachment_api_feeds_only_the_current_model_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "vision-model")
    data_dir = tmp_path / "data"
    client = CapturingRuntimeClient()
    app = create_app(
        build_config(workspace=tmp_path, data_dir=data_dir),
        model_client_factory=lambda _config: client,
    )
    session = post(app, "/api/v1/sessions", json_body={}).json()
    image_data = io.BytesIO()
    Image.new("RGB", (2, 2), color="red").save(image_data, format="PNG")
    image = post_file(
        app,
        f"/api/v1/attachments?session_id={session['id']}",
        filename="screen.png",
        media_type="image/png",
        content=image_data.getvalue(),
    ).json()
    source_text = "private attachment source\n"
    source = post_file(
        app,
        f"/api/v1/attachments?session_id={session['id']}",
        filename="sample.py",
        media_type="text/x-python",
        content=source_text.encode(),
    ).json()

    response = post(
        app,
        "/api/v1/runs",
        json_body={
            "task": "Inspect the supplied files",
            "session_id": session["id"],
            "attachment_ids": [image["id"], source["id"]],
        },
    )

    assert response.status_code == 200
    assert json.loads(response.text.splitlines()[-1])["type"] == "run.completed"
    first_request = client.requests[0]
    current_user = next(
        message for message in reversed(first_request.messages) if message.role == "user"
    )
    assert source_text.strip() in current_user.content
    assert current_user.images[0].data == image_data.getvalue()
    assert current_user.images[0].media_type == "image/png"
    detail = get(app, f"/api/v1/sessions/{session['id']}").json()
    user_message = next(message for message in detail["messages"] if message["role"] == "user")
    assert {item["filename"] for item in user_message["attachments"]} == {
        "screen.png",
        "sample.py",
    }
    assert source_text.strip() not in json.dumps(detail, ensure_ascii=False)
    assert source_text.encode() not in (data_dir / "leanharness.sqlite3").read_bytes()


def test_attachment_api_rejects_cross_session_use_before_creating_a_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "vision-model")
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))
    first = post(app, "/api/v1/sessions", json_body={}).json()
    second = post(app, "/api/v1/sessions", json_body={}).json()
    attachment = post_file(
        app,
        f"/api/v1/attachments?session_id={first['id']}",
        filename="notes.txt",
        media_type="text/plain",
        content=b"session one",
    ).json()

    response = post(
        app,
        "/api/v1/runs",
        json_body={
            "task": "Read it",
            "session_id": second["id"],
            "attachment_ids": [attachment["id"]],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ATTACHMENT"
    assert app.state.store.list_runs(second["id"]) == []


def test_plugin_api_lifecycle_and_permission_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example-model")
    client = CapturingRuntimeClient()
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _config: client,
    )
    plugin_path = Path(__file__).parents[1] / "plugins" / "leanharness-docx"
    installed = post(
        app,
        "/api/v1/plugins/install",
        json_body={"path": str(plugin_path)},
    )
    assert installed.status_code == 200
    assert installed.json()["enabled"] is False
    enabled = post(app, "/api/v1/plugins/leanharness-docx/enable")
    assert enabled.json()["enabled"] is True

    inspect_session = post(app, "/api/v1/sessions", json_body={}).json()
    inspect_run = post(
        app,
        "/api/v1/runs",
        json_body={
            "task": "Inspect",
            "session_id": inspect_session["id"],
            "plugin_ids": ["leanharness-docx"],
        },
    )
    assert inspect_run.status_code == 200
    assert "docx_generate" not in {tool.name for tool in client.requests[0].tools}

    unrestricted_session = post(
        app,
        "/api/v1/sessions",
        json_body={"permission_mode": "unrestricted"},
    ).json()
    unrestricted_run = post(
        app,
        "/api/v1/runs",
        json_body={
            "task": "Inspect with DOCX available",
            "session_id": unrestricted_session["id"],
            "plugin_ids": ["leanharness-docx"],
        },
    )
    assert unrestricted_run.status_code == 200
    assert "docx_generate" in {tool.name for tool in client.requests[2].tools}

    assert post(app, "/api/v1/plugins/leanharness-docx/disable").json()["enabled"] is False
    rejected_session = post(app, "/api/v1/sessions", json_body={}).json()
    rejected = post(
        app,
        "/api/v1/runs",
        json_body={
            "task": "Invalid plugin selection",
            "session_id": rejected_session["id"],
            "plugin_ids": ["leanharness-docx"],
        },
    )
    assert rejected.status_code == 422
    assert app.state.store.list_runs(rejected_session["id"]) == []
    run_id = json.loads(unrestricted_run.text.splitlines()[-1])["run_id"]
    app.state.store.append_event(
        unrestricted_session["id"],
        run_id,
        999,
        "tool.completed",
        {
            "type": "tool.completed",
            "sequence": 999,
            "run_id": run_id,
            "tool": "docx_generate",
            "metadata": {
                "plugin_id": "leanharness-docx",
                "path": "artifacts/report.docx",
                "ok": True,
            },
        },
    )
    assert delete(app, "/api/v1/plugins/leanharness-docx").json()["deleted"] is True
    assert get(app, "/api/v1/plugins").json() == {"plugins": []}
    detail = get(app, f"/api/v1/sessions/{unrestricted_session['id']}").json()
    assert any(
        event.get("tool") == "docx_generate"
        for run in detail["runs"]
        for event in run["trace"]
    )


def test_model_check_contract_does_not_expose_credentials(
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
    assert check.status_code == 200
    assert check.json()["status"] == "ok"
    assert check.json()["model"] == "example-model"
    assert "must-not-leak" not in check.text
    assert post(app, "/api/v1/chat", json_body={"message": "hi"}).status_code == 405


def test_user_input_answer_api_resolves_once_and_validates_answer(tmp_path: Path) -> None:
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    async def scenario():
        coordinator = app.state.user_inputs
        request = coordinator.request(
            run_id="run-1",
            session_id="session-1",
            tool_call_id="call-1",
            question="Which target?",
            options=(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            invalid = await client.post(
                f"/api/v1/runs/run-1/questions/{request.id}", json={"answer": ""}
            )
            resolved = await client.post(
                f"/api/v1/runs/run-1/questions/{request.id}", json={"answer": "API"}
            )
            duplicate = await client.post(
                f"/api/v1/runs/run-1/questions/{request.id}", json={"answer": "Web"}
            )
        return request, invalid, resolved, duplicate, await coordinator.wait(request)

    request, invalid, resolved, duplicate, answer = asyncio.run(scenario())

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "INPUT_INVALID_ANSWER"
    assert resolved.json() == {
        "input_id": request.id,
        "run_id": "run-1",
        "status": "resolved",
    }
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "INPUT_ALREADY_RESOLVED"
    assert answer == "API"


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
    assert created["plan"]["execution_permission_mode"] == "inspect"
    session_id = created["session_id"]
    detail = get(app, f"/api/v1/sessions/{session_id}").json()
    assert any(message.get("kind") == "plan" for message in detail["messages"])
    audit_events = [event for run in detail["runs"] for event in run.get("trace", [])]
    created_audit = next(event for event in audit_events if event["type"] == "plan.created")
    assert created_audit["step_count"] == 1
    assert "source_markdown" not in json.dumps(created_audit, ensure_ascii=False)


def test_plan_resume_explicitly_uses_current_session_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example-model")
    client = PlanResumeModelClient()
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _config: client,
    )
    store = app.state.store
    project = store.ensure_project(tmp_path)
    session = store.create_session(project, permission_mode="inspect")
    plan = store.create_plan(
        session.id,
        title="Create project",
        task="Create a project",
        source_markdown="# Create project\n1. **Create files** - Create app.py",
        steps=(PlanStep("step-1", 1, "Create files", "Create app.py"),),
    )
    run = store.create_run(
        session.id, "plan", plan.task, 24, permission_mode=PermissionMode.INSPECT.value
    )
    store.attach_plan_run(plan.id, run.id)
    store.update_plan_state(plan.id, PlanState.PAUSED)
    store.update_session(session.id, permission_mode="unrestricted")

    response = post(app, f"/api/v1/plans/{plan.id}/resume")
    events = [json.loads(line) for line in response.text.splitlines()]

    assert response.status_code == 200
    assert events[0]["type"] == "run.permission.updated"
    assert events[0]["metadata"] == {
        "previous_permission_mode": "inspect",
        "permission_mode": "unrestricted",
    }
    assert events[-1]["type"] == "run.completed"
    assert client.calls == 3
    assert (tmp_path / "mini-app").is_dir()
    assert (
        get(app, f"/api/v1/plans/{plan.id}").json()["execution_permission_mode"]
        == "unrestricted"
    )
