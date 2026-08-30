from __future__ import annotations

from leanharness.models import ToolCall
from leanharness.runtime.recovery import ModelProtocolRecovery, ToolFailureTracker
from leanharness.tools import ToolErrorInfo, ToolResult


def call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def failed(call_value: ToolCall, code: str) -> ToolResult:
    return ToolResult(
        tool_call_id=call_value.id,
        tool=call_value.name,
        ok=False,
        error=ToolErrorInfo(code, "safe failure", True),
    )


def test_protocol_recovery_is_single_use_and_resettable() -> None:
    recovery = ModelProtocolRecovery()

    repair = recovery.request("zh")

    assert repair is not None
    assert repair.message.role == "user"
    assert "严格 JSON" in repair.message.content
    assert recovery.request("zh") is None
    recovery.reset()
    assert recovery.request("en") is not None


def test_identical_call_repetition_rejects_then_stalls() -> None:
    tracker = ToolFailureTracker()
    calls = [call(f"call-{index}", "workspace_list", path=".") for index in range(4)]

    decisions = [tracker.record_call(item) for item in calls]

    assert decisions[0].reject is False
    assert decisions[1].reject is False
    assert decisions[2].reject is True
    assert decisions[3].terminal_error_code == "RUN_STALLED"
    assert decisions[3].incomplete_reason == "REPEATED_TOOL_CALL"


def test_equivalent_failures_are_grouped_by_target_not_full_arguments() -> None:
    tracker = ToolFailureTracker()
    calls = [
        call(
            f"write-{index}",
            "workspace_write",
            path="result.txt",
            content=f"attempt {index}",
            mode="replace",
        )
        for index in range(3)
    ]

    decisions = [
        tracker.record_result(item, failed(item, "WRITE_STALE")) for item in calls
    ]

    assert decisions[0].guidance is None
    assert decisions[1].guidance is not None
    assert decisions[2].terminal_error_code == "RUN_STALLED"
    assert decisions[2].incomplete_reason == "REPEATED_TOOL_FAILURE"


def test_git_repository_failure_groups_different_operations() -> None:
    tracker = ToolFailureTracker()
    status = call("git-status", "git_inspect", operation="status")
    log = call("git-log", "git_inspect", operation="log")

    first = tracker.record_result(status, failed(status, "GIT_NOT_REPOSITORY"))
    second = tracker.record_result(log, failed(log, "GIT_NOT_REPOSITORY"))

    assert first.guidance is not None
    assert second.terminal_error_code == "GIT_NOT_REPOSITORY"


def test_recovery_guidance_uses_runtime_language() -> None:
    tracker = ToolFailureTracker("zh")
    git_call = call("git-status", "git_inspect", operation="status")

    guidance = tracker.record_result(
        git_call, failed(git_call, "GIT_NOT_REPOSITORY")
    ).guidance

    assert guidance == (
        "当前工作区不是 Git 仓库。不要再次调用 git_inspect\uFF0C"
        "请继续使用工作区工具。"
    )


def test_user_rejections_never_trigger_automatic_stall() -> None:
    tracker = ToolFailureTracker()
    decisions = []
    for index in range(5):
        tool_call = call(
            f"write-{index}",
            "workspace_write",
            path="result.txt",
            content=f"attempt {index}",
            mode="replace",
        )
        decisions.append(
            tracker.record_result(tool_call, failed(tool_call, "APPROVAL_REJECTED"))
        )

    assert all(decision.terminal_error_code is None for decision in decisions)
