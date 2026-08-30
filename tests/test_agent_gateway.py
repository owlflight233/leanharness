import asyncio
import json
from pathlib import Path

import httpx

from leanharness.config import build_config
from leanharness.models import ModelResponse, ToolCall
from leanharness.web.app import create_app


class RunModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="I will inspect the readme.",
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="workspace_read",
                        arguments={"path": "README.md"},
                    ),
                ),
            )
        return ModelResponse(content="The workspace contains a README.")


class MutationModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="I will create the requested directory.",
                tool_calls=(
                    ToolCall(
                        id="mkdir-1",
                        name="workspace_mkdir",
                        arguments={"path": "mini-todo"},
                    ),
                ),
            )
        return ModelResponse(content="The mini-todo directory was created.")


def post(app, path: str, body: dict[str, object]):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body)

    return asyncio.run(request())


def test_run_endpoint_streams_runtime_contract(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("# Ready", encoding="utf-8")
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example")
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _: RunModel(),
    )

    response = post(app, "/api/v1/runs", {"task": "Inspect the repository", "max_steps": 4})
    events = [json.loads(line) for line in response.text.splitlines()]

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert events[0]["type"] == "run.started"
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["answer"] == "The workspace contains a README."
    assert all(event["run_id"] == events[0]["run_id"] for event in events)
    assert any(event["type"] == "tool.completed" for event in events)
    assert events[0]["session_id"]
    assert events[0]["run_id"] == events[-1]["run_id"]
    detail = asyncio.run(_get(app, f"/api/v1/sessions/{events[0]['session_id']}")).json()
    assert detail["session"]["title"] == "Inspect the repository"
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert all(message["run_id"] == events[0]["run_id"] for message in detail["messages"])
    assert detail["runs"][0]["state"] == "COMPLETED"


def test_continuation_uses_original_requirements_and_current_permission(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Ready", encoding="utf-8")
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example")
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _: RunModel(),
    )
    first = post(
        app,
        "/api/v1/runs",
        {"task": "Create a README and run the tests", "max_steps": 4},
    )
    first_events = [json.loads(line) for line in first.text.splitlines()]
    session_id = first_events[0]["session_id"]
    app.state.store.update_session(session_id, permission_mode="unrestricted")

    second = post(
        app,
        "/api/v1/runs",
        {"task": "再试试", "max_steps": 4, "session_id": session_id},
    )
    events = [json.loads(line) for line in second.text.splitlines()]

    assert events[0]["metadata"]["continued"] is True
    assert events[0]["metadata"]["continued_from_run_id"] == first_events[0]["run_id"]
    assert events[0]["metadata"]["permission_mode"] == "unrestricted"
    assert events[0]["metadata"]["session_permission_mode"] == "unrestricted"
    assert events[0]["metadata"]["requirements"] == {
        "mutation_required": True,
        "verification_required": True,
    }
    assert events[-1]["type"] != "run.completed"


def test_unrestricted_continuation_can_apply_original_mutation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example")
    client = MutationModel()
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _: client,
    )
    first = post(app, "/api/v1/runs", {"task": "Create the mini-todo directory"})
    first_events = [json.loads(line) for line in first.text.splitlines()]
    session_id = first_events[0]["session_id"]
    assert first_events[-1]["error"]["code"] == "PERMISSION_INSUFFICIENT"
    app.state.store.update_session(session_id, permission_mode="unrestricted")

    second = post(app, "/api/v1/runs", {"task": "try again", "session_id": session_id})
    events = [json.loads(line) for line in second.text.splitlines()]

    assert events[0]["metadata"]["continued"] is True
    assert events[-1]["type"] == "run.completed"
    assert (tmp_path / "mini-todo").is_dir()
    assert not any(
        event.get("metadata", {}).get("error_code") == "MUTATION_NOT_REQUESTED"
        for event in events
    )


def test_run_endpoint_validates_before_streaming(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LEANHARNESS_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("LEANHARNESS_MODEL_NAME", raising=False)
    app = create_app(build_config(workspace=tmp_path, data_dir=tmp_path / "data"))

    response = post(app, "/api/v1/runs", {"task": "", "max_steps": 1})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RUN_INPUT"


async def _get(app, path: str):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)
