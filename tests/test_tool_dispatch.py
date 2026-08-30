from __future__ import annotations

import asyncio
from pathlib import Path

from leanharness.models import ModelRequest, ModelResponse, ToolCall
from leanharness.permissions import PermissionMode
from leanharness.runtime import CodingAgent
from leanharness.runtime.tool_dispatch import ApprovalPreview, ToolDispatcher
from leanharness.tools import ToolRegistry


def test_dispatcher_converts_preview_failures_to_tool_results(tmp_path: Path) -> None:
    dispatcher = ToolDispatcher(
        ToolRegistry(tmp_path, mode=PermissionMode.UNRESTRICTED), asyncio.Event()
    )
    call = ToolCall(
        id="patch-invalid",
        name="workspace_patch",
        arguments={"patch": "not a unified diff"},
    )

    preview = dispatcher.prepare_approval(call)

    assert not isinstance(preview, ApprovalPreview)
    assert preview.error is not None
    assert preview.error.code == "PATCH_INVALID"
    assert preview.tool_call_id == call.id


def test_dispatcher_previews_and_executes_guarded_read(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    dispatcher = ToolDispatcher(ToolRegistry(tmp_path), asyncio.Event())
    call = ToolCall(
        id="read-ready",
        name="workspace_read",
        arguments={"path": "README.md"},
    )

    async def scenario():
        result = await dispatcher.execute(call)
        return result

    result = asyncio.run(scenario())

    assert result.ok is True
    assert result.tool_call_id == call.id


class CancellingModel:
    def __init__(self, cancel_event: asyncio.Event) -> None:
        self.cancel_event = cancel_event
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.cancel_event.set()
        return ModelResponse(
            content="I will inspect both files.",
            tool_calls=(
                ToolCall("one", "workspace_list", {"path": "."}),
                ToolCall("two", "workspace_search", {"query": "x", "path": "."}),
            ),
        )


def test_cancellation_closes_all_assistant_tool_calls_with_results(tmp_path: Path) -> None:
    cancel_event = asyncio.Event()
    model = CancellingModel(cancel_event)
    agent = CodingAgent(tmp_path, model, cancel_event=cancel_event, max_steps=4)

    async def scenario():
        return [event async for event in agent.run("Inspect the workspace")]

    events = asyncio.run(scenario())

    assert events[-1].type == "run.cancelled"
    completed = [event for event in events if event.type == "tool.completed"]
    requested = [event for event in events if event.type == "tool.requested"]
    assert [event.tool for event in requested] == ["workspace_list", "workspace_search"]
    assert [event.metadata["tool_call_id"] for event in completed] == ["one", "two"]
    assert all(event.metadata["error_code"] == "TOOL_CANCELLED" for event in completed)
    tool_messages = [message for message in agent.context.messages if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["one", "two"]
