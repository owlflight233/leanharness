"""Bounded coding-agent loop owned by the LeanHarness runtime core."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Protocol

from leanharness.context import (
    ContextBudgetError,
    ContextProjection,
    ContextSource,
    ContextStore,
)
from leanharness.errors import (
    ApprovalExpiredError,
    ModelContextLengthError,
    ModelError,
    ModelProtocolError,
)
from leanharness.models import ModelMessage, ModelRequest, ModelResponse, ToolCall
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.runtime.completion import CompletionLedger
from leanharness.runtime.events import RuntimeEvent, RuntimeEventType
from leanharness.runtime.metrics import RunMetrics
from leanharness.runtime.outcome import (
    OUTCOME_TOOL,
    OUTCOME_TOOL_NAME,
    OutcomeProtocolError,
    OutcomeStatus,
    parse_outcome,
)
from leanharness.runtime.prompting import system_prompt
from leanharness.runtime.recovery import ModelProtocolRecovery, ToolFailureTracker
from leanharness.runtime.state import RunState, transition
from leanharness.tools import ToolErrorInfo, ToolExecutionError, ToolRegistry, ToolResult

DEFAULT_MAX_STEPS = 24
MIN_MAX_STEPS = 2
MAX_MAX_STEPS = 64
MAX_TASK_CHARS = 32_000
MAX_TOOL_CALLS_PER_STEP = 4
MAX_STEP_TOOL_RESULT_BYTES = 96 * 1024
MAX_PROGRESS_CHARS = 200
_HAN_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")


class RunControlError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RuntimeModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class CodingAgent:
    def __init__(
        self,
        workspace: Path,
        model_client: RuntimeModelClient,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        context_chars: int = 160_000,
        run_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        tool_registry_factory: Callable[[Path], ToolRegistry] = ToolRegistry,
        language: str = "same",
        permission_mode: PermissionMode = PermissionMode.INSPECT,
        session_id: str = "ephemeral",
        approvals: ApprovalCoordinator | None = None,
        history: tuple[ModelMessage, ...] = (),
        history_sources: tuple[ContextSource, ...] = (),
        initial_sequence: int = 0,
        reserve_summary_round: bool = True,
        include_outcome_tool: bool = True,
        context_sanitizer: Callable[[str], str] | None = None,
    ) -> None:
        if not MIN_MAX_STEPS <= max_steps <= MAX_MAX_STEPS:
            raise ValueError(f"max_steps must be between {MIN_MAX_STEPS} and {MAX_MAX_STEPS}")
        self.workspace = workspace.resolve(strict=True)
        self.model_client = model_client
        self.max_steps = max_steps
        self.run_id = run_id or uuid.uuid4().hex
        self.cancel_event = cancel_event or asyncio.Event()
        self.tools = tool_registry_factory(self.workspace, mode=permission_mode)
        self.context = ContextStore(
            max_chars=context_chars, summary_sanitizer=context_sanitizer
        )
        self.state = RunState.CREATED
        self.language = language
        self.permission_mode = permission_mode
        self.session_id = session_id
        self.approvals = approvals
        self.history_sources = history_sources or tuple(
            ContextSource(f"history:{index}", message)
            for index, message in enumerate(history)
        )
        self.reserve_summary_round = reserve_summary_round
        self.include_outcome_tool = include_outcome_tool
        self._sequence = initial_sequence
        self.evidence = CompletionLedger()
        self.metrics = RunMetrics()
        self._protocol_recovery = ModelProtocolRecovery()
        self._failure_tracker = ToolFailureTracker()

    async def run(self, task: str) -> AsyncIterator[RuntimeEvent]:
        self._protocol_recovery.reset()
        async for event in self._run_task(task, initialize_context=True):
            yield event

    async def continue_task(
        self,
        task: str,
    ) -> AsyncIterator[RuntimeEvent]:
        if self.state not in {
            RunState.COMPLETED,
            RunState.EXHAUSTED,
            RunState.FAILED,
            RunState.CANCELLED,
        }:
            raise RunControlError("RUN_NOT_TERMINAL", "Previous task is still active")
        self.state = RunState.CREATED
        self.evidence = CompletionLedger()
        self._failure_tracker.reset()
        self._protocol_recovery.reset()
        async for event in self._run_task(task, initialize_context=False):
            yield event

    def set_event_sequence(self, sequence: int) -> None:
        if sequence < self._sequence:
            raise ValueError("Runtime event sequence cannot move backwards")
        self._sequence = sequence

    def checkpoint_context(self, summary: str) -> None:
        """Keep system constraints and a bounded summary between plan steps."""
        system_messages = tuple(
            message for message in self.context.messages if message.role == "system"
        )
        self.context.replace(
            (*system_messages, ModelMessage(role="user", content=summary))
        )
        self.history_sources = ()

    async def _run_task(
        self,
        task: str,
        *,
        initialize_context: bool,
    ) -> AsyncIterator[RuntimeEvent]:
        validated_task = validate_run_task(task)
        self.state = transition(self.state, RunState.PREPARING)
        if initialize_context:
            self.context.append(
                ModelMessage(
                    role="system",
                    content=system_prompt(self.language, self.permission_mode),
                )
            )
        self.context.append(
            ModelMessage(role="user", content=validated_task)
        )
        yield self._event(
            "run.started",
            summary=_run_started_summary(self.language),
            metadata={"permission_mode": self.permission_mode.value},
        )

        for step in range(1, self.max_steps + 1):
            if self.cancel_event.is_set():
                self.state = transition(self.state, RunState.CANCELLED)
                yield self._event(
                    "run.cancelled",
                    step=step,
                    summary=_cancelled_summary(self.language),
                    metadata=self._terminal_metadata(
                        incomplete_reason="RUN_CANCELLED"
                    ),
                )
                return
            yield self._event(
                "step.started",
                step=step,
                summary=_step_started_summary(step, self.language),
            )
            summary_round = self.reserve_summary_round and step == self.max_steps
            try:
                projection = await self.context.projector.project_async(
                    self.history_sources,
                    self.context,
                    self.model_client,
                )
                self.metrics.record_projection(
                    chars=projection.projected_chars,
                    messages=len(projection.messages),
                    compressed_steps=projection.compressed_steps,
                    compressed_tool_results=projection.compressed_messages,
                    semantic_calls=self.context.projector.semantic_calls,
                    semantic_fallback=projection.semantic_fallback,
                    generation=projection.generation,
                )
                yield self._context_projected_event(step, projection)
                if projection.changed or projection.semantic_fallback:
                    yield self._context_compacted_event(step, projection)
                self.state = transition(self.state, RunState.REQUESTING_MODEL)
                self.metrics.model_calls += 1
                try:
                    response = await self.model_client.complete(
                        self._model_request(projection.messages, summary_round)
                    )
                except ModelContextLengthError:
                    recovered = await self.context.projector.project_async(
                        self.history_sources,
                        self.context,
                        self.model_client,
                        force_semantic=True,
                    )
                    if recovered.digest == projection.digest:
                        raise ContextBudgetError(
                            "Context compaction did not produce a smaller request"
                        ) from None
                    self.metrics.record_projection(
                        chars=recovered.projected_chars,
                        messages=len(recovered.messages),
                        compressed_steps=recovered.compressed_steps,
                        compressed_tool_results=recovered.compressed_messages,
                        semantic_calls=self.context.projector.semantic_calls,
                        semantic_fallback=recovered.semantic_fallback,
                        generation=recovered.generation,
                    )
                    yield self._context_compacted_event(step, recovered)
                    self.metrics.model_calls += 1
                    try:
                        response = await self.model_client.complete(
                            self._model_request(recovered.messages, summary_round)
                        )
                    except ModelContextLengthError as exc:
                        raise ContextBudgetError(
                            "Model context window remained exceeded after one recovery"
                        ) from exc
                self.state = transition(self.state, RunState.INTERPRETING)
            except asyncio.CancelledError:
                self.state = transition(self.state, RunState.CANCELLED)
                yield self._event(
                    "run.cancelled",
                    step=step,
                    summary=_cancelled_summary(self.language),
                    metadata=self._terminal_metadata(
                        incomplete_reason="RUN_CANCELLED"
                    ),
                )
                return
            except ContextBudgetError as exc:
                self.state = transition(self.state, RunState.FAILED)
                yield self._event(
                    "context.compaction.failed",
                    step=step,
                    error_code="CONTEXT_BUDGET_EXCEEDED",
                    error_message=str(exc),
                )
                yield self._event(
                    "run.failed",
                    step=step,
                    error_code="CONTEXT_BUDGET_EXCEEDED",
                    error_message=str(exc),
                    metadata=self._terminal_metadata(
                        incomplete_reason="CONTEXT_BUDGET_EXCEEDED"
                    ),
                )
                return
            except ModelProtocolError:
                repair = self._protocol_recovery.request(self.language)
                if repair is not None:
                    self.context.append(repair.message)
                    self.state = transition(self.state, RunState.PREPARING)
                    yield self._event(
                        "assistant.progress",
                        step=step,
                        summary=repair.public_summary,
                    )
                    yield self._event("step.completed", step=step)
                    continue
                self.state = transition(self.state, RunState.FAILED)
                yield self._event(
                    "run.failed",
                    step=step,
                    error_code="MODEL_PROTOCOL_ERROR",
                    error_message="Model returned an invalid response twice",
                    metadata=self._terminal_metadata(
                        incomplete_reason="MODEL_PROTOCOL_ERROR"
                    ),
                )
                return
            except ModelError as exc:
                self.state = transition(self.state, RunState.FAILED)
                yield self._event(
                    "run.failed",
                    step=step,
                    error_code=exc.code,
                    error_message=exc.message,
                    metadata=self._terminal_metadata(incomplete_reason=exc.code),
                )
                return
            except Exception:
                self.state = transition(self.state, RunState.FAILED)
                yield self._event(
                    "run.failed",
                    step=step,
                    error_code="RUN_MODEL_FAILED",
                    error_message="Model request failed safely",
                    metadata=self._terminal_metadata(
                        incomplete_reason="RUN_MODEL_FAILED"
                    ),
                )
                return

            self.context.append(
                ModelMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )
            if response.usage:
                self.metrics.record_usage(response.usage)
                yield self._event(
                    "usage.reported", step=step, usage=response.usage.to_dict()
                )

            if summary_round:
                self.state = transition(self.state, RunState.EXHAUSTED)
                yield self._event(
                    "run.incomplete",
                    step=step,
                    answer=response.content.strip() or None,
                    summary=_incomplete_summary(self.language),
                    metadata=self._terminal_metadata(
                        incomplete_reason="STEP_BUDGET_EXHAUSTED",
                    ),
                )
                return

            calls = response.tool_calls
            if len(calls) == 1 and calls[0].name == OUTCOME_TOOL_NAME:
                call = calls[0]
                yield self._event(
                    "tool.requested",
                    step=step,
                    tool=call.name,
                    metadata={"tool_call_id": call.id},
                )
                try:
                    outcome = parse_outcome(call)
                except OutcomeProtocolError as exc:
                    result = _runtime_tool_error(call, exc.code, exc.message)
                else:
                    if outcome.status is OutcomeStatus.INCOMPLETE:
                        self.state = transition(self.state, RunState.EXHAUSTED)
                        yield self._event(
                            "run.incomplete",
                            step=step,
                            answer=outcome.answer,
                            summary=_incomplete_summary(self.language),
                            metadata=self._terminal_metadata(
                                incomplete_reason="MODEL_REPORTED_INCOMPLETE"
                            ),
                        )
                        return
                    decision = self.evidence.validate_completed()
                    if decision.accepted:
                        self.state = transition(self.state, RunState.COMPLETED)
                        yield self._event(
                            "tool.completed",
                            step=step,
                            tool=call.name,
                            metadata={"tool_call_id": call.id, "ok": True},
                        )
                        yield self._event(
                            "run.completed",
                            step=step,
                            answer=outcome.answer,
                            summary=_completed_summary(self.language),
                            metadata=self._terminal_metadata(),
                        )
                        return
                    result = _runtime_tool_error(
                        call,
                        decision.reason or "OUTCOME_CONTRADICTS_EVIDENCE",
                        decision.guidance or "The reported outcome contradicts tool evidence",
                    )
                if result is not None:
                    yield self._event(
                        "tool.completed",
                        step=step,
                        tool=call.name,
                        metadata={
                            **result.public_metadata,
                            "tool_call_id": call.id,
                            "ok": result.ok,
                        },
                    )
                self.context.append(
                    ModelMessage(
                        role="tool",
                        content=result.to_model_content(),
                        tool_call_id=call.id,
                    )
                )
                yield self._event(
                    "assistant.progress",
                    step=step,
                    summary=_outcome_retry_summary(self.language),
                )
                yield self._event("step.completed", step=step)
                self.state = transition(self.state, RunState.PREPARING)
                continue
            if calls:
                yield self._event(
                    "assistant.progress",
                    step=step,
                    summary=_progress_summary(response.content, calls[0], self.language),
                )
                step_result_bytes = 0
                recovery_guidance: list[str] = []
                for call_index, call in enumerate(calls):
                    self.metrics.tool_calls += 1
                    requested_metadata = {
                        "tool_call_id": call.id,
                        **_safe_arguments(call),
                    }
                    yield self._event(
                        "tool.requested",
                        step=step,
                        tool=call.name,
                        metadata=requested_metadata,
                    )
                    if call_index >= MAX_TOOL_CALLS_PER_STEP:
                        result = _runtime_tool_error(
                            call,
                            "TOOL_CALL_LIMIT",
                            "Only the first four tool calls are executed in one step; "
                            "request remaining work in the next step",
                        )
                    else:
                        repetition = self._failure_tracker.record_call(call)
                        if repetition.terminal_error_code:
                            self.state = transition(self.state, RunState.FAILED)
                            yield self._event(
                                "run.failed",
                                step=step,
                                error_code=repetition.terminal_error_code,
                                error_message=repetition.terminal_message,
                                metadata=self._terminal_metadata(
                                    incomplete_reason=(
                                        repetition.incomplete_reason
                                        or repetition.terminal_error_code
                                    )
                                ),
                            )
                            return
                        if repetition.reject:
                            result = _runtime_tool_error(
                                call,
                                "TOOL_REPEATED",
                                "Identical tool call was already attempted twice",
                            )
                        else:
                            result = None
                            if call.name == OUTCOME_TOOL_NAME:
                                result = _runtime_tool_error(
                                    call,
                                    "OUTCOME_MUST_BE_ALONE",
                                    "report_run_outcome must be the only call in a response",
                                )
                            elif self.tools.approval_required(call):
                                if self.approvals is None:
                                    result = _runtime_tool_error(
                                        call,
                                        "APPROVAL_UNAVAILABLE",
                                        "Interactive approval is unavailable",
                                    )
                                else:
                                    try:
                                        preview_data = self.tools.preview(call)
                                    except ToolExecutionError as exc:
                                        result = _runtime_tool_error(
                                            call,
                                            exc.code,
                                            exc.message,
                                            recoverable=exc.recoverable,
                                        )
                                    except Exception:
                                        result = _runtime_tool_error(
                                            call,
                                            "APPROVAL_PREVIEW_FAILED",
                                            "A safe approval preview could not be created",
                                        )
                                    if result is None:
                                        expected_hashes = preview_data.pop("target_hashes", None)
                                        raw_preview = preview_data.pop("preview", None)
                                        request = self.approvals.request(
                                            run_id=self.run_id,
                                            session_id=self.session_id,
                                            tool_call_id=call.id,
                                            tool_name=call.name,
                                            summary=_approval_summary(call, self.language),
                                            parameters=preview_data,
                                            preview=raw_preview,
                                        )
                                        self.state = transition(
                                            self.state, RunState.WAITING_APPROVAL
                                        )
                                        yield self._event(
                                            "approval.required",
                                            step=step,
                                            tool=call.name,
                                            summary=request.summary,
                                            metadata={
                                                "approval_id": request.id,
                                                "tool_call_id": call.id,
                                                "parameters": request.parameters,
                                                "preview": request.preview,
                                            },
                                        )
                                        try:
                                            decision = await self.approvals.wait(request)
                                        except ApprovalExpiredError:
                                            decision = "reject"
                                            result = _runtime_tool_error(
                                                call,
                                                "APPROVAL_TIMEOUT",
                                                "Approval was not decided within 15 minutes",
                                            )
                                        except asyncio.CancelledError:
                                            self.state = transition(
                                                self.state, RunState.CANCELLED
                                            )
                                            yield self._event(
                                                "run.cancelled",
                                                step=step,
                                                summary=_cancelled_summary(self.language),
                                                metadata=self._terminal_metadata(
                                                    incomplete_reason="RUN_CANCELLED"
                                                ),
                                            )
                                            return
                                        yield self._event(
                                            "approval.resolved",
                                            step=step,
                                            tool=call.name,
                                            metadata={
                                                "approval_id": request.id,
                                                "decision": decision,
                                            },
                                        )
                                        if result is None and decision == "reject":
                                            result = _runtime_tool_error(
                                                call,
                                                "APPROVAL_REJECTED",
                                                "The user rejected this tool call",
                                            )
                                        if result is None:
                                            self.state = transition(
                                                self.state, RunState.EXECUTING_TOOL
                                            )
                                            yield self._event(
                                                "tool.started",
                                                step=step,
                                                tool=call.name,
                                                metadata={"tool_call_id": call.id},
                                            )
                                            result = await self._execute_tool(
                                                call,
                                                approved=True,
                                                expected_hashes=(
                                                    expected_hashes
                                                    if isinstance(expected_hashes, dict)
                                                    else None
                                                ),
                                            )
                            else:
                                self.state = transition(
                                    self.state, RunState.EXECUTING_TOOL
                                )
                                yield self._event(
                                    "tool.started",
                                    step=step,
                                    tool=call.name,
                                    metadata={"tool_call_id": call.id},
                                )
                                result = await self._execute_tool(call)
                            assert result is not None
                    content = result.to_model_content()
                    content_bytes = len(content.encode("utf-8"))
                    if step_result_bytes + content_bytes > MAX_STEP_TOOL_RESULT_BYTES:
                        result = _runtime_tool_error(
                            call,
                            "TOOL_RESULT_BUDGET",
                            "Step tool-result budget was exhausted",
                        )
                        content = result.to_model_content()
                        content_bytes = len(content.encode("utf-8"))
                    step_result_bytes += content_bytes
                    self.evidence.record(call.name, result)
                    failure = self._failure_tracker.record_result(call, result)
                    if failure.guidance:
                        recovery_guidance.append(failure.guidance)
                    self.context.append(
                        ModelMessage(role="tool", content=content, tool_call_id=call.id)
                    )
                    yield self._event(
                        "tool.completed",
                        step=step,
                        tool=call.name,
                        metadata={
                            **result.public_metadata,
                            **(
                                {
                                    "error_code": result.error.code,
                                    "recoverable": result.error.recoverable,
                                }
                                if result.error
                                else {}
                            ),
                            "tool_call_id": call.id,
                            "ok": result.ok,
                        },
                    )
                    if failure.terminal_error_code:
                        self.state = transition(self.state, RunState.FAILED)
                        yield self._event(
                            "run.failed",
                            step=step,
                            error_code=failure.terminal_error_code,
                            error_message=failure.terminal_message,
                            metadata=self._terminal_metadata(
                                incomplete_reason=(
                                    failure.incomplete_reason
                                    or failure.terminal_error_code
                                )
                            ),
                        )
                        return
                if recovery_guidance:
                    self.context.append(
                        ModelMessage(role="user", content="\n".join(recovery_guidance))
                    )
                self.state = transition(self.state, RunState.PREPARING)
                yield self._event("step.completed", step=step)
                continue

            if response.content.strip():
                decision = self.evidence.validate_completed()
                if decision.accepted:
                    self.state = transition(self.state, RunState.COMPLETED)
                    yield self._event(
                        "run.completed",
                        step=step,
                        answer=response.content.strip(),
                        summary=_completed_summary(self.language),
                        metadata=self._terminal_metadata(),
                    )
                    return
                self.context.append(
                    ModelMessage(
                        role="user",
                        content=(
                            decision.guidance
                            or "The reported answer conflicts with observed tool facts; "
                            "continue or report incomplete."
                        ),
                    )
                )
                self.state = transition(self.state, RunState.PREPARING)
                yield self._event(
                    "assistant.progress",
                    step=step,
                    summary=_outcome_retry_summary(self.language),
                )
                yield self._event("step.completed", step=step)
                continue

            self.context.append(
                ModelMessage(
                    role="user",
                    content=(
                        "Choose the next tool action, or call report_run_outcome alone to "
                        "explicitly report completed or incomplete."
                    ),
                )
            )
            self.state = transition(self.state, RunState.PREPARING)
            yield self._event(
                "assistant.progress",
                step=step,
                summary=_outcome_required_summary(self.language),
            )
            yield self._event("step.completed", step=step)

        # PlanController disables the per-step summary round so one budget is
        # shared across the whole plan. Exhaustion still needs a terminal event.
        self.state = transition(self.state, RunState.EXHAUSTED)
        yield self._event(
            "run.incomplete",
            step=self.max_steps,
            summary=_incomplete_summary(self.language),
            metadata=self._terminal_metadata(
                incomplete_reason="STEP_BUDGET_EXHAUSTED",
            ),
        )

    def _terminal_metadata(
        self,
        *,
        incomplete_reason: str | None = None,
    ) -> dict[str, object]:
        return {
            "evidence": self.evidence.public_summary(),
            "metrics": self.metrics.to_dict(),
            "context": self.metrics.context_dict(),
            **({"incomplete_reason": incomplete_reason} if incomplete_reason else {}),
        }

    def _model_request(
        self, messages: tuple[ModelMessage, ...], summary_round: bool
    ) -> ModelRequest:
        return ModelRequest(
            messages=messages,
            max_tokens=2_048,
            tools=(
                ()
                if summary_round
                else (
                    (*self.tools.definitions, OUTCOME_TOOL)
                    if self.include_outcome_tool
                    else self.tools.definitions
                )
            ),
            tool_choice="none" if summary_round else "auto",
        )

    def _context_projected_event(
        self, step: int, projection: ContextProjection
    ) -> RuntimeEvent:
        return self._event(
            "context.projected",
            step=step,
            summary=(
                "已生成本轮模型上下文" if self.language == "zh" else "Model context projected"
            ),
            metadata={
                "projected_chars": projection.projected_chars,
                "projected_messages": len(projection.messages),
                "source_count": len(projection.source_ids),
                "context_generation": projection.generation,
                "projection_hash": projection.digest,
            },
        )

    def _context_compacted_event(
        self, step: int, projection: ContextProjection
    ) -> RuntimeEvent:
        return self._event(
            "context.compacted",
            step=step,
            summary=(
                "已压缩本轮模型上下文" if self.language == "zh" else "Model context compacted"
            ),
            metadata={
                "projected_chars": projection.projected_chars,
                "compressed_steps": projection.compressed_steps,
                "compressed_tool_results": projection.compressed_messages,
                "semantic_compacted": projection.semantic_compacted,
                "semantic_fallback": projection.semantic_fallback,
                "context_generation": projection.generation,
                "projection_hash": projection.digest,
            },
        )

    async def _execute_tool(
        self,
        call: ToolCall,
        *,
        approved: bool = False,
        expected_hashes: dict[str, str | None] | None = None,
    ) -> ToolResult:
        try:
            if approved:
                return await asyncio.to_thread(
                    self.tools.execute_approved,
                    call,
                    expected_hashes=expected_hashes,
                    cancel_signal=self.cancel_event,
                )
            return await asyncio.to_thread(
                self.tools.execute,
                call,
                cancel_signal=self.cancel_event,
            )
        except asyncio.CancelledError:
            self.cancel_event.set()
            raise

    def _event(self, event_type: RuntimeEventType, **kwargs) -> RuntimeEvent:
        event = RuntimeEvent(
            type=event_type,
            sequence=self._sequence,
            run_id=self.run_id,
            **kwargs,
        )
        self._sequence += 1
        return event


def validate_run_task(task: str) -> str:
    if not isinstance(task, str) or not task.strip():
        raise RunControlError("INVALID_RUN_TASK", "Task must not be blank")
    if len(task) > MAX_TASK_CHARS:
        raise RunControlError(
            "INVALID_RUN_TASK",
            f"Task must not exceed {MAX_TASK_CHARS} characters",
        )
    return task


def _progress_summary(content: str, call: ToolCall, language: str = "same") -> str:
    normalized = " ".join(content.strip().split())
    forbidden = ("```", "chain of thought", "reasoning:", "思考过程")
    if (
        normalized
        and len(normalized) <= MAX_PROGRESS_CHARS
        and len(content.splitlines()) <= 2
        and not any(marker in normalized.casefold() for marker in forbidden)
        and _progress_language_matches(normalized, language)
    ):
        return normalized
    return _fallback_summary(call, language)


def _progress_language_matches(text: str, language: str) -> bool:
    if language == "same":
        return True
    han = len(_HAN_CHAR.findall(text))
    latin = len(_LATIN_CHAR.findall(text))
    if language == "zh":
        return han > 0 and han * 2 >= latin
    if language == "en":
        return latin > 0 and latin >= han * 2
    return True


def _fallback_summary(call: ToolCall, language: str = "same") -> str:
    target = call.arguments.get("path", ".")
    if language == "zh":
        summaries = {
            "workspace_list": f"检查 {target} 下的项目结构。",
            "workspace_read": f"读取 {target} 以核对实现细节。",
            "workspace_search": f"在 {target} 下定位相关代码。",
            "workspace_mkdir": f"准备创建目录 {target}。",
            "workspace_patch": "准备应用受控补丁。",
            "workspace_write": f"准备写入文件 {target}。",
            "workspace_edit": f"准备编辑文件 {target}。",
            "workspace_command": "准备运行受控验证命令。",
            "git_inspect": "检查当前 Git 状态和差异。",
        }
        return summaries.get(call.name, f"执行工具 {call.name}。")
    if language == "same":
        target_text = str(target)
        return f"[{call.name}] path={target_text}"
    if call.name == "workspace_list":
        return f"I will inspect the structure under {target} to choose the next target."
    if call.name == "workspace_read":
        return f"I will read {target} to verify the relevant implementation details."
    if call.name == "workspace_search":
        return f"I will search under {target} to locate the relevant code."
    if call.name == "workspace_mkdir":
        return f"I will create the guarded workspace directory {target}."
    if call.name == "workspace_patch":
        return "I will apply a guarded workspace patch."
    if call.name == "workspace_write":
        return f"I will write the workspace file {target}."
    if call.name == "workspace_edit":
        return f"I will edit the workspace file {target}."
    if call.name == "workspace_command":
        return "I will run a guarded project verification command."
    if call.name == "git_inspect":
        return "I will inspect the current Git state and changes."
    return f"I will run the {call.name} tool."


def _safe_arguments(call: ToolCall) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key in (
        "path",
        "start_line",
        "line_count",
        "max_depth",
        "case_sensitive",
        "max_results",
        "profile",
        "timeout_seconds",
        "operation",
        "revision",
        "parents",
        "mode",
        "expected_sha256",
        "end_line",
        "create_parents",
    ):
        if key in call.arguments and isinstance(call.arguments[key], str | int | bool):
            safe[key] = call.arguments[key]
    if isinstance(query := call.arguments.get("query"), str):
        safe["query"] = query[:120]
    return safe


def _approval_summary(call: ToolCall, language: str) -> str:
    if language == "zh":
        summaries = {
            "workspace_mkdir": "需要批准创建工作区目录。",
            "workspace_patch": "需要批准补丁写入。",
            "workspace_write": "需要批准写入工作区文件。",
            "workspace_edit": "需要批准编辑工作区文件。",
            "workspace_command": "需要批准验证命令。",
        }
        return summaries.get(call.name, "需要批准此工具操作。")
    summaries = {
        "workspace_mkdir": "Approval is required to create this workspace directory.",
        "workspace_patch": "Approval is required to apply this patch.",
        "workspace_write": "Approval is required to write this workspace file.",
        "workspace_edit": "Approval is required to edit this workspace file.",
        "workspace_command": "Approval is required to run this verification command.",
    }
    return summaries.get(call.name, "Approval is required for this tool action.")


def _run_started_summary(language: str) -> str:
    return "编码任务已开始" if language == "zh" else "Coding run started"


def _completed_summary(language: str) -> str:
    return "任务已完成" if language == "zh" else "Coding run completed"


def _cancelled_summary(language: str) -> str:
    return "任务已取消" if language == "zh" else "Run cancelled"


def _step_started_summary(step: int, language: str) -> str:
    return (
        f"第 {step} 步: 选择下一个编码动作"
        if language == "zh"
        else f"Step {step}: select the next coding action"
    )


def _incomplete_summary(language: str) -> str:
    return (
        "运行预算已用完, 返回未完成总结"
        if language == "zh"
        else "Run budget reached; returning an incomplete summary"
    )


def _outcome_required_summary(language: str) -> str:
    return (
        "请通过完成控制动作明确报告任务结果"
        if language == "zh"
        else "Report the task result through the explicit outcome action"
    )


def _outcome_retry_summary(language: str) -> str:
    return (
        "完成控制动作与已观察到的工具事实不一致, 请继续处理或报告未完成"
        if language == "zh"
        else "The outcome conflicts with observed tool facts; continue or report incomplete"
    )


# Backward-compatible name for integrations created before coding tools were enabled.
ReadOnlyAgent = CodingAgent


def _runtime_tool_error(
    call: ToolCall,
    code: str,
    message: str,
    *,
    recoverable: bool = True,
) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id,
        tool=call.name,
        ok=False,
        error=ToolErrorInfo(code=code, message=message, recoverable=recoverable),
        public_metadata={"error_code": code, "recoverable": recoverable},
    )
