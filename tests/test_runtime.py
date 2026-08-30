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
    ReadOnlyAgent,
    RunControlError,
    RunState,
    validate_run_task,
)
from leanharness.runtime.outcome import OUTCOME_TOOL_NAME
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


def test_runtime_accepts_model_completion_without_application_intent_rules(tmp_path: Path) -> None:
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
    assert len(model.requests) == 1


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


def test_repeated_git_inspection_stops_in_non_repository_workspace(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("git-1", "git_inspect", {"operation": "status"}),
            tool_response("git-2", "git_inspect", {"operation": "log"}),
        ]
    )

    events = collect(ReadOnlyAgent(tmp_path, model), "Inspect the repository")

    assert events[-1].type == "run.failed"
    assert events[-1].error_code == "GIT_NOT_REPOSITORY"
    completed = [event for event in events if event.type == "tool.completed"]
    assert [event.metadata["error_code"] for event in completed] == [
        "GIT_NOT_REPOSITORY",
        "GIT_NOT_REPOSITORY",
    ]
    assert len(model.requests) == 2


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


def test_progress_summary_falls_back_when_model_uses_wrong_language(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example", encoding="utf-8")
    model = ScriptedModel(
        [
            tool_response(
                "call-1",
                "workspace_read",
                {"path": "README.md"},
                "I'll gather more evidence before drafting the answer.",
            ),
            ModelResponse(content="读取完成。"),
        ]
    )

    events = collect(CodingAgent(tmp_path, model, language="zh"))

    progress = next(event for event in events if event.type == "assistant.progress")
    assert progress.summary == "读取 README.md 以核对实现细节。"


def outcome_response(status: str, answer: str) -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(
            ToolCall(
                id="outcome-1",
                name=OUTCOME_TOOL_NAME,
                arguments={"status": status, "answer": answer},
            ),
        ),
    )


def test_model_owns_completion_decision_after_observation(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1", "workspace_list", {"path": "."}),
            outcome_response("completed", "The workspace was inspected."),
        ]
    )

    events = collect(ReadOnlyAgent(tmp_path, model))

    assert events[-1].type == "run.completed"
    assert events[-1].answer == "The workspace was inspected."
    assert model.requests[-1].tools[-1].name == OUTCOME_TOOL_NAME


def test_model_can_report_incomplete_without_keyword_inference(tmp_path: Path) -> None:
    model = ScriptedModel([outcome_response("incomplete", "The requested change is blocked.")])

    events = collect(
        CodingAgent(tmp_path, model, permission_mode=PermissionMode.INSPECT),
        "Please handle the repository as needed.",
    )

    assert events[-1].type == "run.incomplete"
    assert events[-1].answer == "The requested change is blocked."


def test_plain_text_is_a_model_completion_decision(tmp_path: Path) -> None:
    model = ScriptedModel([ModelResponse(content="The task is complete.")])

    events = collect(ReadOnlyAgent(tmp_path, model))

    assert events[-1].type == "run.completed"
    assert events[-1].answer == "The task is complete."


def test_inspect_permission_does_not_classify_task_before_model_request(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response(
                "patch-1",
                "workspace_patch",
                {"patch": "*** Begin Patch\n*** End Patch"},
            ),
            outcome_response("incomplete", "The requested edit is not available in inspect mode."),
        ]
    )

    events = collect(
        CodingAgent(tmp_path, model, permission_mode=PermissionMode.INSPECT),
        "Update the repository.",
    )

    assert model.requests
    assert events[0].type == "run.started"
    assert any(
        event.type == "tool.requested" and event.tool == "workspace_patch"
        for event in events
    )
    assert not any(event.error_code == "PERMISSION_INSUFFICIENT" for event in events)


def test_failed_mutation_contradicts_completed_outcome(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response(
                "patch-1",
                "workspace_patch",
                {"patch": "*** Begin Patch\n*** End Patch"},
            ),
            outcome_response("completed", "The file was updated."),
            outcome_response("incomplete", "The update is blocked."),
        ]
    )

    events = collect(
        CodingAgent(tmp_path, model, permission_mode=PermissionMode.UNRESTRICTED),
        "Handle the requested change.",
    )

    assert any(
        event.type == "tool.completed"
        and event.metadata
        and event.metadata.get("error_code") == "PATCH_INVALID"
        for event in events
    )
    assert events[-1].type == "run.incomplete"

@pytest.mark.parametrize("task", ["", "   ", "x" * 32_001])
def test_run_task_is_bounded(task: str) -> None:
    with pytest.raises(RunControlError):
        validate_run_task(task)


def test_state_machine_rejects_invalid_transition() -> None:
    assert transition(RunState.CREATED, RunState.PREPARING) is RunState.PREPARING
    with pytest.raises(InvalidTransition):
        transition(RunState.CREATED, RunState.COMPLETED)
