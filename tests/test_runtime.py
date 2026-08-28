from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from leanharness.errors import ModelUnavailableError
from leanharness.models import ModelRequest, ModelResponse, ModelUsage, ToolCall
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.runtime import (
    CodingAgent,
    ContinuationContext,
    ReadOnlyAgent,
    RunControlError,
    RunState,
    validate_run_task,
)
from leanharness.runtime.state import InvalidTransition, transition


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def tool_response(call_id: str, name: str, arguments: dict[str, object], content: str = ""):
    return ModelResponse(
        content=content,
        finish_reason="tool_calls",
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
    )


def collect(agent: ReadOnlyAgent, task: str = "Inspect this repository"):
    async def run():
        return [event async for event in agent.run(task)]

    return asyncio.run(run())


def test_runtime_executes_tool_observes_result_and_accepts_final_answer(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example", encoding="utf-8")
    model = ScriptedModel(
        [
            tool_response(
                "call-1",
                "workspace_read",
                {"path": "README.md"},
                "I will inspect the project readme.",
            ),
            ModelResponse(
                content="The repository contains an Example README.",
                finish_reason="stop",
                usage=ModelUsage(total_tokens=20),
            ),
        ]
    )

    agent = ReadOnlyAgent(tmp_path, model, run_id="run-1")
    events = collect(agent)

    assert agent.state is RunState.COMPLETED
    assert events[-1].type == "run.completed"
    assert events[-1].answer == "The repository contains an Example README."
    assert [event.sequence for event in events] == list(range(len(events)))
    assert any(event.type == "assistant.progress" for event in events)
    completed = next(event for event in events if event.type == "tool.completed")
    assert completed.metadata == {
        "path": "README.md",
        "start_line": 1,
        "line_count": 1,
        "truncated": False,
        "tool_call_id": "call-1",
        "ok": True,
    }
    tool_message = model.requests[1].messages[-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "call-1"
    assert "# Example" in tool_message.content
    assert "# Example" not in json.dumps([event.to_dict() for event in events])


def test_runtime_rejects_early_completion_until_workspace_is_observed(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelResponse(content="Done without inspection."),
            tool_response("call-1", "workspace_list", {"path": "."}),
            ModelResponse(content="The workspace is empty."),
        ]
    )

    agent = ReadOnlyAgent(tmp_path, model, max_steps=4)
    events = collect(agent)

    assert events[-1].type == "run.completed"
    assert len(model.requests) == 3
    assert "verifiable evidence" in model.requests[1].messages[-1].content


def test_mutation_task_without_successful_patch_is_not_completed(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    model = ScriptedModel(
        [
            tool_response("read-1", "workspace_read", {"path": "README.md"}),
            tool_response(
                "patch-1",
                "workspace_patch",
                {"patch": "*** Begin Patch\n*** End Patch"},
            ),
            ModelResponse(content="I could not apply the requested edit."),
            ModelResponse(content="The edit remains incomplete."),
        ]
    )

    events = collect(
        CodingAgent(
            tmp_path,
            model,
            max_steps=4,
            permission_mode=PermissionMode.UNRESTRICTED,
        ),
        "Update README.md",
    )

    assert events[-1].type == "run.incomplete"
    assert not any(event.type == "run.completed" for event in events)
    failed_patch = next(
        event
        for event in events
        if event.type == "tool.completed" and event.tool == "workspace_patch"
    )
    assert failed_patch.metadata["error_code"] == "PATCH_INVALID"


def test_approval_preview_preserves_safe_patch_error(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response(
                "patch-1",
                "workspace_patch",
                {"patch": "*** Begin Patch\n*** End Patch"},
            ),
            ModelResponse(content="The malformed patch was not applied."),
            ModelResponse(content="The requested edit remains incomplete."),
        ]
    )

    events = collect(
        CodingAgent(
            tmp_path,
            model,
            max_steps=3,
            permission_mode=PermissionMode.APPROVE,
            approvals=ApprovalCoordinator(timeout_seconds=1),
        ),
        "Create result.txt",
    )

    completed = next(event for event in events if event.type == "tool.completed")
    assert completed.metadata["error_code"] == "PATCH_INVALID"
    assert not any(event.type == "approval.required" for event in events)
    tool_message = model.requests[1].messages[-1]
    assert json.loads(tool_message.content)["error"]["code"] == "PATCH_INVALID"


def test_runtime_injects_bounded_continuation_capsule(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1", "workspace_list", {"path": "."}),
            ModelResponse(content="Permission is now unrestricted."),
        ]
    )
    continuation = ContinuationContext(
        previous_task="Create a small example file",
        previous_state="EXHAUSTED",
        changed_files=("example.py",),
        incomplete_reason="PATCH_INVALID",
        permission_mode="unrestricted",
    )

    events = collect(
        CodingAgent(tmp_path, model, continuation=continuation),
        "I changed the permission. What about now?",
    )

    assert events[-1].type == "run.completed"
    capsule = model.requests[0].messages[1].content
    assert "Create a small example file" in capsule
    assert "EXHAUSTED" in capsule
    assert "example.py" in capsule
    assert len(capsule.encode("utf-8")) <= 4096


def test_runtime_reserves_last_step_for_incomplete_summary(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1", "workspace_list", {"path": "."}),
            ModelResponse(content="Observed the workspace, but analysis is incomplete."),
        ]
    )

    agent = ReadOnlyAgent(tmp_path, model, max_steps=2)
    events = collect(agent)

    assert agent.state is RunState.EXHAUSTED
    assert events[-1].type == "run.incomplete"
    assert events[-1].answer == "Observed the workspace, but analysis is incomplete."
    assert model.requests[-1].tools == ()
    assert model.requests[-1].tool_choice == "none"


def test_explicit_verification_task_requires_successful_command(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1", "workspace_list", {"path": "."}),
            ModelResponse(content="The workspace was inspected without running tests."),
            ModelResponse(content="Tests were not run; verification is incomplete."),
        ]
    )

    events = collect(ReadOnlyAgent(tmp_path, model, max_steps=3), "Run the tests")

    assert events[-1].type == "run.incomplete"
    assert events[-1].metadata["incomplete_reason"] == "VERIFICATION_NOT_RUN"


def test_terminal_event_reports_efficiency_metrics(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1", "workspace_list", {"path": "."}),
            ModelResponse(
                content="Inspection complete.",
                usage=ModelUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
            ),
        ]
    )

    events = collect(ReadOnlyAgent(tmp_path, model, max_steps=3))

    assert events[-1].metadata["metrics"] == {
        "model_calls": 2,
        "tool_calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }


def test_third_identical_call_is_recoverable_and_fourth_stalls(tmp_path: Path) -> None:
    responses = [
        tool_response(f"call-{index}", "workspace_list", {"path": "."})
        for index in range(1, 5)
    ]
    model = ScriptedModel(responses)

    events = collect(ReadOnlyAgent(tmp_path, model, max_steps=6))

    assert events[-1].type == "run.failed"
    assert events[-1].error_code == "RUN_STALLED"
    third_result = model.requests[3].messages[-1]
    assert third_result.role == "tool"
    assert json.loads(third_result.content)["error"]["code"] == "TOOL_REPEATED"


def test_tool_error_returns_to_model_and_can_recover(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1", "workspace_read", {"path": "missing.py"}),
            tool_response("call-2", "workspace_list", {"path": "."}),
            ModelResponse(content="The requested file does not exist and the workspace is empty."),
        ]
    )

    events = collect(ReadOnlyAgent(tmp_path, model, max_steps=4))

    assert events[-1].type == "run.completed"
    first_result = model.requests[1].messages[-1]
    assert json.loads(first_result.content)["error"]["code"] == "PATH_NOT_FOUND"


def test_tool_call_overflow_returns_recoverable_results_and_can_continue(tmp_path: Path) -> None:
    calls = tuple(
        ToolCall(
            id=f"call-{index}",
            name="workspace_list",
            arguments={"path": ".", "max_depth": index},
        )
        for index in range(1, 6)
    )
    model = ScriptedModel(
        [
            ModelResponse(content="I will inspect the workspace.", tool_calls=calls),
            ModelResponse(content="The workspace inspection is complete."),
        ]
    )

    events = collect(ReadOnlyAgent(tmp_path, model, max_steps=3))

    assert events[-1].type == "run.completed"
    tool_messages = [message for message in model.requests[1].messages if message.role == "tool"]
    assert len(tool_messages) == 5
    assert all(json.loads(message.content)["ok"] for message in tool_messages[:4])
    overflow = json.loads(tool_messages[4].content)
    assert overflow["error"]["code"] == "TOOL_CALL_LIMIT"
    assert overflow["error"]["recoverable"] is True
    completed = [event for event in events if event.type == "tool.completed"]
    assert completed[-1].metadata == {
        "error_code": "TOOL_CALL_LIMIT",
        "recoverable": True,
        "tool_call_id": "call-5",
        "ok": False,
    }


def test_runtime_maps_model_failure_and_cancellation(tmp_path: Path) -> None:
    failed = collect(
        ReadOnlyAgent(tmp_path, ScriptedModel([ModelUnavailableError("offline")]))
    )
    assert failed[-1].type == "run.failed"
    assert failed[-1].error_code == "MODEL_UNAVAILABLE"

    cancel_event = asyncio.Event()
    cancel_event.set()
    cancelled = collect(
        ReadOnlyAgent(
            tmp_path,
            ScriptedModel([]),
            cancel_event=cancel_event,
        )
    )
    assert cancelled[-1].type == "run.cancelled"


def test_same_language_fallback_is_neutral_metadata(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response(
                "call-1",
                "workspace_read",
                {"path": "README.md"},
                "```private reasoning```",
            ),
            ModelResponse(content="Ответ готов.", finish_reason="stop"),
        ]
    )
    events = collect(ReadOnlyAgent(tmp_path, model, language="same"))
    progress = next(event for event in events if event.type == "assistant.progress")
    assert progress.summary == "[workspace_read] path=README.md"


@pytest.mark.parametrize("task", ["", "   ", "x" * 32_001])
def test_run_task_is_bounded(task: str) -> None:
    with pytest.raises(RunControlError):
        validate_run_task(task)


def test_state_machine_rejects_invalid_transition() -> None:
    assert transition(RunState.CREATED, RunState.PREPARING) is RunState.PREPARING
    with pytest.raises(InvalidTransition):
        transition(RunState.CREATED, RunState.COMPLETED)
