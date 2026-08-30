"""Bounded protocol and tool-failure recovery owned by the fixed runtime core."""

from __future__ import annotations

import json
from dataclasses import dataclass

from leanharness.models import ModelMessage, ToolCall
from leanharness.tools import ToolResult


@dataclass(frozen=True, slots=True)
class ProtocolRepair:
    message: ModelMessage
    public_summary: str


class ModelProtocolRecovery:
    """Permit one safe correction without retaining malformed provider output."""

    def __init__(self, language: str = "same") -> None:
        self._language = language
        self._used = False

    def reset(self) -> None:
        self._used = False

    def request(self, language: str) -> ProtocolRepair | None:
        if self._used:
            return None
        self._used = True
        return ProtocolRepair(
            message=ModelMessage(role="user", content=_protocol_repair_prompt(language)),
            public_summary=_protocol_repair_summary(language),
        )


@dataclass(frozen=True, slots=True)
class RepetitionDecision:
    reject: bool = False
    terminal_error_code: str | None = None
    terminal_message: str | None = None
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class FailureDecision:
    guidance: str | None = None
    terminal_error_code: str | None = None
    terminal_message: str | None = None
    incomplete_reason: str | None = None


class ToolFailureTracker:
    """Bound retries by public operation identity without choosing the next action."""

    def __init__(self, language: str = "same") -> None:
        self._language = language
        self._repeat_key: tuple[str, str] | None = None
        self._repeat_count = 0
        self._failure_counts: dict[tuple[str, str, str], int] = {}

    def reset(self) -> None:
        self._repeat_key = None
        self._repeat_count = 0
        self._failure_counts.clear()

    def record_call(self, call: ToolCall) -> RepetitionDecision:
        key = (call.name, _stable_arguments(call))
        if key == self._repeat_key:
            self._repeat_count += 1
        else:
            self._repeat_key, self._repeat_count = key, 1
        if self._repeat_count >= 4:
            return RepetitionDecision(
                terminal_error_code="RUN_STALLED",
                terminal_message="Repeated identical tool calls",
                incomplete_reason="REPEATED_TOOL_CALL",
            )
        if self._repeat_count == 3:
            return RepetitionDecision(reject=True)
        return RepetitionDecision()

    def record_result(self, call: ToolCall, result: ToolResult) -> FailureDecision:
        if result.ok or result.error is None:
            return FailureDecision()
        code = result.error.code
        key = _failure_key(call, code)
        count = self._failure_counts.get(key, 0) + 1
        self._failure_counts[key] = count
        if code == "GIT_NOT_REPOSITORY":
            if count >= 2:
                return FailureDecision(
                    guidance=_git_guidance(self._language),
                    terminal_error_code="GIT_NOT_REPOSITORY",
                    terminal_message=_git_terminal_message(self._language),
                    incomplete_reason="GIT_NOT_REPOSITORY",
                )
            return FailureDecision(guidance=_git_guidance(self._language))
        if _should_stall_on_tool_error(code) and count >= 3:
            return FailureDecision(
                terminal_error_code="RUN_STALLED",
                terminal_message=_stall_terminal_message(self._language),
                incomplete_reason="REPEATED_TOOL_FAILURE",
            )
        if count == 2:
            return FailureDecision(
                guidance=_repeat_failure_guidance(call.name, code, self._language)
            )
        return FailureDecision()


def _protocol_repair_prompt(language: str) -> str:
    if language == "zh":
        return (
            "上一轮模型响应无法按协议解析。请重新选择下一步: 工具调用必须是严格 JSON "
            "对象并完全符合工具参数定义; 如果无法安全调用工具, 请使用 "
            "report_run_outcome 报告 incomplete。不要重复输出解释性文本代替工具参数。"
        )
    return (
        "The previous model response could not be parsed by the tool protocol. Choose the "
        "next action again: tool arguments must be a strict JSON object matching the "
        "tool schema. If no safe tool call is possible, use report_run_outcome with "
        "status=incomplete. Do not emit explanatory text in place of tool arguments."
    )


def _protocol_repair_summary(language: str) -> str:
    return (
        "模型响应格式无效, 已请求一次协议修正"
        if language == "zh"
        else "Model response format was invalid; one protocol correction was requested"
    )


def _stable_arguments(call: ToolCall) -> str:
    return json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _failure_key(call: ToolCall, error_code: str) -> tuple[str, str, str]:
    if error_code == "GIT_NOT_REPOSITORY":
        return call.name, error_code, "<workspace>"
    resource_parts: list[str] = []
    for name in ("path", "profile", "operation", "query"):
        value = call.arguments.get(name)
        if isinstance(value, str):
            resource_parts.append(f"{name}={value[:160]}")
    resource = "|".join(resource_parts) or "<operation>"
    return call.name, error_code, resource


def _should_stall_on_tool_error(error_code: str) -> bool:
    if error_code in {
        "APPROVAL_REJECTED",
        "APPROVAL_TIMEOUT",
        "APPROVAL_UNAVAILABLE",
        "TOOL_RESULT_BUDGET",
        "TOOL_CALL_LIMIT",
    }:
        return False
    return error_code.startswith(
        ("PATCH_", "WRITE_", "EDIT_", "DIRECTORY_", "PATH_")
    ) or error_code == "TOOL_INVALID_ARGUMENTS"


def _git_guidance(language: str = "same") -> str:
    if language == "zh":
        return "当前工作区不是 Git 仓库。不要再次调用 git_inspect\uFF0C请继续使用工作区工具。"
    return (
        "This workspace is not a Git repository. Do not call git_inspect again; "
        "continue with workspace tools."
    )


def _repeat_failure_guidance(tool: str, code: str, language: str) -> str:
    if language == "zh":
        return (
            f"{tool} 工具已因 {code} 连续失败两次。不要重复相同方法\uFF1B请重新读取相关状态\uFF0C"
            "修正参数\uFF0C或在最终未完成摘要中说明阻塞原因。"
        )
    return (
        f"The {tool} tool has failed twice with {code}. Do not repeat the same approach. "
        "Re-read the relevant state, correct the arguments, or explain the blocker in the "
        "final incomplete summary."
    )


def _git_terminal_message(language: str) -> str:
    if language == "zh":
        return "当前工作区不是 Git 仓库\uFF0C重复的 Git 检查已停止。"
    return "The workspace is not a Git repository; repeated Git inspection was stopped."


def _stall_terminal_message(language: str) -> str:
    if language == "zh":
        return "相同目标的工具失败重复出现\uFF0C运行已停止。"
    return "The same tool failure recurred for the same target"
