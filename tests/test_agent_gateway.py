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
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
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


def test_follow_up_is_verbatim_and_receives_public_history(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Ready", encoding="utf-8")
    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example")
    model = RunModel()
    app = create_app(
        build_config(workspace=tmp_path, data_dir=tmp_path / "data"),
        model_client_factory=lambda _: model,
    )
    first = post(app, "/api/v1/runs", {"task": "Inspect the repository", "max_steps": 4})
    first_events = [json.loads(line) for line in first.text.splitlines()]
    session_id = first_events[0]["session_id"]
    second = post(app, "/api/v1/runs", {"task": "继续", "max_steps": 4, "session_id": session_id})
    events = [json.loads(line) for line in second.text.splitlines()]

    assert events[0]["metadata"] == {"permission_mode": "inspect"}
    assert "continued" not in events[0]["metadata"]
    assert len(model.requests) >= 3
    second_request = model.requests[2]
    assert [message.content for message in second_request.messages[-2:]] == [
        "The workspace contains a README.",
        "继续",
    ]


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
