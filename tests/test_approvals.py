from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from leanharness.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    RunConflictError,
)
from leanharness.models import ModelRequest, ModelResponse, ToolCall
from leanharness.permissions import ActiveRunRegistry, ApprovalCoordinator, PermissionMode
from leanharness.runtime import CodingAgent, RunState
from leanharness.storage import LocalStore


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses[len(self.requests) - 1]


def patch_response() -> ModelResponse:
    return ModelResponse(
        content="I will update the file.",
        tool_calls=(
            ToolCall(
                id="patch-1",
                name="workspace_patch",
                arguments={
                    "patch": "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-before\n+after\n"
                },
            ),
        ),
    )


def test_approve_mode_waits_and_applies_one_tool_call(tmp_path: Path) -> None:
    source = tmp_path / "value.txt"
    source.write_text("before\n", encoding="utf-8")

    async def scenario():
        coordinator = ApprovalCoordinator(timeout_seconds=1)
        model = ScriptedModel([patch_response(), ModelResponse(content="Updated and verified.")])
        agent = CodingAgent(
            tmp_path,
            model,
            max_steps=3,
            permission_mode=PermissionMode.APPROVE,
            approvals=coordinator,
            session_id="session-1",
        )
        events = []
        async for event in agent.run("Update value.txt"):
            events.append(event)
            if event.type == "approval.required":
                coordinator.resolve(
                    agent.run_id,
                    str(event.metadata["approval_id"]),
                    "approve",
                )
        return agent, model, events

    agent, _, events = asyncio.run(scenario())
    assert source.read_text(encoding="utf-8") == "after\n"
    assert agent.state is RunState.COMPLETED
    assert [event.type for event in events if event.type.startswith("approval.")] == [
        "approval.required",
        "approval.resolved",
    ]


def test_approve_mode_creates_new_file_from_valid_unified_diff(tmp_path: Path) -> None:
    response = ModelResponse(
        content="I will create the requested file.",
        tool_calls=(
            ToolCall(
                id="patch-create",
                name="workspace_patch",
                arguments={
                    "patch": "--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1 @@\n+created\n"
                },
            ),
        ),
    )

    async def scenario():
        coordinator = ApprovalCoordinator(timeout_seconds=1)
        model = ScriptedModel([response, ModelResponse(content="Created created.txt.")])
        agent = CodingAgent(
            tmp_path,
            model,
            max_steps=3,
            permission_mode=PermissionMode.APPROVE,
            approvals=coordinator,
        )
        events = []
        async for event in agent.run("Create created.txt"):
            events.append(event)
            if event.type == "approval.required":
                coordinator.resolve(agent.run_id, str(event.metadata["approval_id"]), "approve")
        return events

    events = asyncio.run(scenario())
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created\n"
    assert events[-1].type == "run.completed"


def test_rejected_tool_returns_recoverable_result_to_model(tmp_path: Path) -> None:
    source = tmp_path / "value.txt"
    source.write_text("before\n", encoding="utf-8")

    async def scenario():
        coordinator = ApprovalCoordinator(timeout_seconds=1)
        model = ScriptedModel(
            [
                patch_response(),
                ModelResponse(
                    content="The requested edit was rejected, so the file remains unchanged."
                ),
            ]
        )
        agent = CodingAgent(
            tmp_path,
            model,
            max_steps=3,
            permission_mode=PermissionMode.APPROVE,
            approvals=coordinator,
        )
        async for event in agent.run("Update value.txt"):
            if event.type == "approval.required":
                coordinator.resolve(agent.run_id, str(event.metadata["approval_id"]), "reject")
        return model

    model = asyncio.run(scenario())
    assert source.read_text(encoding="utf-8") == "before\n"
    tool_message = model.requests[1].messages[-1]
    assert json.loads(tool_message.content)["error"]["code"] == "APPROVAL_REJECTED"


def test_approval_is_single_use_and_active_runs_are_per_session(tmp_path: Path) -> None:
    async def scenario():
        coordinator = ApprovalCoordinator()
        request = coordinator.request(
            run_id="run-1",
            session_id="session-1",
            tool_call_id="call-1",
            tool_name="workspace_command",
            summary="verify",
            parameters={"profile": "pytest"},
            preview=None,
        )
        coordinator.resolve("run-1", request.id, "approve")
        with pytest.raises(ApprovalAlreadyResolvedError):
            coordinator.resolve("run-1", request.id, "reject")

    asyncio.run(scenario())
    registry = ActiveRunRegistry()
    registry.acquire("session-1", "run-1")
    with pytest.raises(RunConflictError):
        registry.acquire("session-1", "run-2")
    registry.acquire("session-2", "run-3")


def test_approval_persistence_redacts_preview_and_restart_interrupts(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path), permission_mode="approve")
    run = store.create_run(
        session.id, "coding", "edit", 4, permission_mode="approve"
    )
    store.create_approval(
        "approval-1",
        run.id,
        "call-1",
        "workspace_patch",
        {
            "summary": "edit",
            "parameters": {"files": ["value.txt"]},
            "preview": "PRIVATE DIFF CONTENT",
        },
    )
    assert "PRIVATE DIFF CONTENT" not in json.dumps(store.get_approval("approval-1").request)
    assert store.interrupt_active_runs() == 1
    assert store.list_runs(session.id)[0].error_code == "RUN_INTERRUPTED"
    assert store.get_approval("approval-1").state == "EXPIRED"


def test_approval_timeout_is_persisted_as_expired(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path), permission_mode="approve")
    run = store.create_run(session.id, "coding", "edit", 4, permission_mode="approve")

    async def scenario() -> str:
        coordinator = ApprovalCoordinator(
            timeout_seconds=0,
            on_request=lambda request: store.create_approval(
                request.id,
                request.run_id,
                request.tool_call_id,
                request.tool_name,
                {"summary": request.summary},
            ),
            on_expire=lambda request: store.expire_approval(request.id),
        )
        request = coordinator.request(
            run_id=run.id,
            session_id=session.id,
            tool_call_id="call-1",
            tool_name="workspace_patch",
            summary="edit",
            parameters={"files": ["value.txt"]},
            preview=None,
        )
        with pytest.raises(ApprovalExpiredError):
            await coordinator.wait(request)
        return request.id

    approval_id = asyncio.run(scenario())
    assert store.get_approval(approval_id).state == "EXPIRED"
