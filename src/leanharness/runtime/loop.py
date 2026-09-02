"""Bounded coding-agent loop owned by the LeanHarness runtime core."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Protocol

from leanharness.context import (
    ContextBudgetError,
    ContextProjection,
    ContextProtocolError,
    ContextSource,
    ContextStore,
)
from leanharness.errors import ApprovalExpiredError, ModelError, ModelProtocolError
from leanharness.models import ModelMessage, ModelRequest, ModelResponse, ToolCall
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.runtime.completion import CompletionLedger
from leanharness.runtime.delegation import (
    CHILD_MAX_STEPS,
    MAX_PARALLEL_SUBTASKS,
    ParallelAnalysisTool,
    ScopedReadOnlyToolRegistry,
    SubtaskRequest,
    SubtaskResult,
    SubtaskStatus,
    SubtaskUsage,
    parse_worker_answer,
    worker_system_prompt,
)
from leanharness.runtime.events import RuntimeEvent, RuntimeEventType
from leanharness.runtime.metrics import RunMetrics
from leanharness.runtime.model_step import (
    ModelStepExecutor,
    ProjectionSignal,
    ProtocolRepairSignal,
    RequestStartedSignal,
    ResponseSignal,
)
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
from leanharness.runtime.tool_dispatch import ToolDispatcher, tool_error_result
from leanharness.runtime.user_input import (
    REQUEST_USER_INPUT_TOOL,
    REQUEST_USER_INPUT_TOOL_NAME,
    UserInputCoordinator,
    UserInputExpiredError,
    UserInputProtocolError,
    parse_user_input_call,
)
from leanharness.tools import ToolExecutionError, ToolRegistry, ToolResult

DEFAULT_MAX_STEPS = 24
MIN_MAX_STEPS = 2
MAX_MAX_STEPS = 64
MAX_TASK_CHARS = 32_000
MAX_TOOL_CALLS_PER_STEP = 4
MAX_STEP_TOOL_RESULT_BYTES = 96 * 1024
MAX_PROGRESS_CHARS = 200
# Leave room for thinking plus structured tool arguments. The provider may
# impose a lower model-specific ceiling, but 2,048 was too small for DOCX calls.
MODEL_OUTPUT_TOKEN_BUDGET = 16_384
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
        max_steps: int | None = DEFAULT_MAX_STEPS,
        context_chars: int = 160_000,
        run_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        tool_registry_factory: Callable[[Path], ToolRegistry] = ToolRegistry,
        language: str = "same",
        permission_mode: PermissionMode = PermissionMode.INSPECT,
        session_id: str = "ephemeral",
        approvals: ApprovalCoordinator | None = None,
        user_inputs: UserInputCoordinator | None = None,
        history: tuple[ModelMessage, ...] = (),
        history_sources: tuple[ContextSource, ...] = (),
        initial_sequence: int = 0,
        reserve_summary_round: bool = True,
        include_outcome_tool: bool = True,
        context_sanitizer: Callable[[str], str] | None = None,
        metrics: RunMetrics | None = None,
        user_message: ModelMessage | None = None,
        enable_delegation: bool = False,
        system_message: str | None = None,
        model_output_tokens: int = MODEL_OUTPUT_TOKEN_BUDGET,
        summary_outcome_only: bool = False,
    ) -> None:
        if max_steps is not None and not MIN_MAX_STEPS <= max_steps <= MAX_MAX_STEPS:
            raise ValueError(f"max_steps must be between {MIN_MAX_STEPS} and {MAX_MAX_STEPS}")
        self.workspace = workspace.resolve(strict=True)
        self.model_client = model_client
        self.max_steps = max_steps
        self.run_id = run_id or uuid.uuid4().hex
        self.cancel_event = cancel_event or asyncio.Event()
        self.tools = tool_registry_factory(self.workspace, mode=permission_mode)
        self._delegation_enabled = enable_delegation
        self._context_sanitizer = context_sanitizer
        self._system_message = system_message
        self._model_output_tokens = model_output_tokens
        self._summary_outcome_only = summary_outcome_only
        if enable_delegation:
            self.tools.register(
                ParallelAnalysisTool(self.workspace, self._run_delegated_analysis)
            )
        self._tool_dispatcher = ToolDispatcher(self.tools, self.cancel_event)
        self.context = ContextStore(
            max_chars=context_chars, summary_sanitizer=context_sanitizer
        )
        self.state = RunState.CREATED
        self.language = language
        self.permission_mode = permission_mode
        self.session_id = session_id
        self.approvals = approvals
        self.user_inputs = user_inputs
        self.history_sources = history_sources or tuple(
            ContextSource(f"history:{index}", message)
            for index, message in enumerate(history)
        )
        self.reserve_summary_round = reserve_summary_round
        self.include_outcome_tool = include_outcome_tool
        self._sequence = initial_sequence
        self.evidence = CompletionLedger()
        self.metrics = metrics or RunMetrics()
        self._protocol_recovery = ModelProtocolRecovery()
        if user_message is not None and user_message.role != "user":
            raise ValueError("Initial runtime message must have the user role")
        self._initial_user_message = user_message
        self._failure_tracker = ToolFailureTracker(self.language)
        # Tools can become unavailable after a deterministic environment fact
        # (for example, git_inspect in a non-repository workspace).  The model
        # still owns the next action; this set only projects the facts observed
        # by the runtime into subsequent tool definitions.
        self._disabled_tools: set[str] = set()
        self._delegated_subtasks = 0
        self._model_step = ModelStepExecutor(
            context=self.context,
            model_client=self.model_client,
            metrics=self.metrics,
            protocol_recovery=self._protocol_recovery,
            request_builder=self._model_request,
            language=self.language,
        )

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
        self._disabled_tools.clear()
        self._delegated_subtasks = 0
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
                    content=(
                        self._system_message
                        or system_prompt(
                            self.language,
                            self.permission_mode,
                            delegation=self._delegation_enabled,
                        )
                    ),
                )
            )
        self.context.append(
            self._initial_user_message
            if initialize_context and self._initial_user_message is not None
            else ModelMessage(role="user", content=validated_task)
        )
        self._initial_user_message = None
        yield self._event(
            "run.started",
            summary=_run_started_summary(self.language),
            metadata={"permission_mode": self.permission_mode.value},
        )

        step = 0
        while self.max_steps is None or step < self.max_steps:
            step += 1
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
            summary_round = (
                self.reserve_summary_round
                and self.max_steps is not None
                and step == self.max_steps
            )
            response: ModelResponse | None = None
            protocol_repaired = False
            try:
                async for signal in self._model_step.execute(
                    history_sources=self.history_sources,
                    summary_round=summary_round,
                ):
                    if isinstance(signal, ProjectionSignal):
                        if signal.compacted:
                            yield self._context_compacted_event(step, signal.projection)
                        else:
                            yield self._context_projected_event(step, signal.projection)
                    elif isinstance(signal, RequestStartedSignal):
                        self.state = transition(self.state, RunState.REQUESTING_MODEL)
                    elif isinstance(signal, ProtocolRepairSignal):
                        self.state = transition(self.state, RunState.PREPARING)
                        yield self._event(
                            "assistant.progress",
                            step=step,
                            summary=signal.repair.public_summary,
                        )
                        yield self._event("step.completed", step=step)
                        protocol_repaired = True
                    elif isinstance(signal, ResponseSignal):
                        response = signal.response
                        self.state = transition(self.state, RunState.INTERPRETING)
                if protocol_repaired:
                    continue
                if response is None:
                    raise RuntimeError(
                        "Model step ended without a response or protocol repair"
                    )
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
            except ContextProtocolError as exc:
                self.state = transition(self.state, RunState.FAILED)
                yield self._event(
                    "context.compaction.failed",
                    step=step,
                    error_code="CONTEXT_PROTOCOL_ERROR",
                    error_message=str(exc),
                )
                yield self._event(
                    "run.failed",
                    step=step,
                    error_code="CONTEXT_PROTOCOL_ERROR",
                    error_message="Projected model context contained an invalid tool sequence",
                    metadata=self._terminal_metadata(
                        incomplete_reason="CONTEXT_PROTOCOL_ERROR"
                    ),
                )
                return
            except ModelProtocolError:
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
            if len(calls) == 1 and calls[0].name == REQUEST_USER_INPUT_TOOL_NAME:
                call = calls[0]
                yield self._event(
                    "tool.requested",
                    step=step,
                    tool=call.name,
                    metadata={"tool_call_id": call.id},
                )
                result: ToolResult | None = None
                try:
                    question, options = parse_user_input_call(call)
                except UserInputProtocolError as exc:
                    result = tool_error_result(call, exc.code, exc.message)
                if result is None and self.user_inputs is None:
                    result = tool_error_result(
                        call,
                        "INPUT_UNAVAILABLE",
                        "Interactive user input is unavailable",
                    )
                if result is None:
                    assert self.user_inputs is not None
                    request = self.user_inputs.request(
                        run_id=self.run_id,
                        session_id=self.session_id,
                        tool_call_id=call.id,
                        question=question,
                        options=options,
                    )
                    self.state = transition(self.state, RunState.WAITING_INPUT)
                    yield self._event(
                        "input.required",
                        step=step,
                        tool=call.name,
                        metadata={
                            "input_id": request.id,
                            "tool_call_id": call.id,
                            "question": request.question,
                            "options": [
                                {
                                    "label": option.label,
                                    "description": option.description,
                                }
                                for option in request.options
                            ],
                        },
                    )
                    try:
                        answer = await self.user_inputs.wait(request)
                    except UserInputExpiredError:
                        result = tool_error_result(
                            call,
                            "INPUT_TIMEOUT",
                            "User input was not provided before the timeout",
                        )
                    except asyncio.CancelledError:
                        self.state = transition(self.state, RunState.CANCELLED)
                        for event in self._cancel_pending_tool_calls(
                            calls, 0, step, current_requested=True
                        ):
                            yield event
                        yield self._event(
                            "run.cancelled",
                            step=step,
                            summary=_cancelled_summary(self.language),
                            metadata=self._terminal_metadata(
                                incomplete_reason="RUN_CANCELLED"
                            ),
                        )
                        return
                    else:
                        yield self._event(
                            "input.resolved",
                            step=step,
                            tool=call.name,
                            metadata={"input_id": request.id, "tool_call_id": call.id},
                        )
                        result = ToolResult(
                            tool_call_id=call.id,
                            tool=call.name,
                            ok=True,
                            data={"answer": answer},
                            public_metadata={"input_id": request.id},
                        )
                assert result is not None
                self.context.append(
                    ModelMessage(
                        role="tool",
                        content=result.to_model_content(),
                        tool_call_id=call.id,
                    )
                )
                yield self._event(
                    "tool.completed",
                    step=step,
                    tool=call.name,
                    metadata={
                        **result.public_metadata,
                        **(
                            {"error_code": result.error.code}
                            if result.error is not None
                            else {}
                        ),
                        "tool_call_id": call.id,
                        "ok": result.ok,
                    },
                )
                self.state = transition(self.state, RunState.PREPARING)
                yield self._event("step.completed", step=step)
                continue
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
                    # A control tool call is still an assistant tool call. Close
                    # it with a matching tool result before changing run state so
                    # every projected context remains provider-protocol legal.
                    result = ToolResult(
                        tool_call_id=call.id,
                        tool=call.name,
                        ok=True,
                        data={
                            "status": outcome.status.value,
                            "answer": outcome.answer,
                        },
                        public_metadata={"status": outcome.status.value},
                    )
                    if outcome.status is OutcomeStatus.INCOMPLETE:
                        self.context.append(
                            ModelMessage(
                                role="tool",
                                content=result.to_model_content(),
                                tool_call_id=call.id,
                            )
                        )
                        yield self._event(
                            "tool.completed",
                            step=step,
                            tool=call.name,
                            metadata={
                                **result.public_metadata,
                                "tool_call_id": call.id,
                                "ok": True,
                            },
                        )
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
                    decision = self.evidence.validate_completed(language=self.language)
                    if decision.accepted:
                        self.context.append(
                            ModelMessage(
                                role="tool",
                                content=result.to_model_content(),
                                tool_call_id=call.id,
                            )
                        )
                        self.state = transition(self.state, RunState.COMPLETED)
                        yield self._event(
                            "tool.completed",
                            step=step,
                            tool=call.name,
                            metadata={
                                **result.public_metadata,
                                "tool_call_id": call.id,
                                "ok": True,
                            },
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
                    if self.cancel_event.is_set():
                        self.state = transition(self.state, RunState.CANCELLED)
                        for event in self._cancel_pending_tool_calls(
                            calls, call_index, step, current_requested=True
                        ):
                            yield event
                        yield self._event(
                            "run.cancelled",
                            step=step,
                            summary=_cancelled_summary(self.language),
                            metadata=self._terminal_metadata(
                                incomplete_reason="RUN_CANCELLED"
                            ),
                        )
                        return
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
                        elif call.name in self._disabled_tools:
                            result = _runtime_tool_error(
                                call,
                                "TOOL_UNAVAILABLE",
                                _disabled_tool_message(call.name, self.language),
                            )
                        else:
                            result = None
                            registered_tool = self.tools.get(call.name)
                            if call.name == OUTCOME_TOOL_NAME:
                                result = _runtime_tool_error(
                                    call,
                                    "OUTCOME_MUST_BE_ALONE",
                                    "report_run_outcome must be the only call in a response",
                                )
                            elif isinstance(registered_tool, ParallelAnalysisTool):
                                self.state = transition(
                                    self.state, RunState.EXECUTING_TOOL
                                )
                                yield self._event(
                                    "tool.started",
                                    step=step,
                                    tool=call.name,
                                    metadata={"tool_call_id": call.id},
                                )
                                try:
                                    subtask_requests = registered_tool.prepare(call)
                                except ToolExecutionError as exc:
                                    result = registered_tool.error_result(call, exc)
                                else:
                                    remaining = MAX_PARALLEL_SUBTASKS - self._delegated_subtasks
                                    if len(subtask_requests) > remaining:
                                        result = registered_tool.error_result(
                                            call,
                                            ToolExecutionError(
                                                "SUBTASK_LIMIT",
                                                "A parent run may delegate at most five subtasks",
                                            ),
                                        )
                                        subtask_requests = ()
                                    else:
                                        self._delegated_subtasks += len(subtask_requests)
                                if result is None and subtask_requests:
                                    for request in subtask_requests:
                                        yield self._event(
                                            "subtask.requested",
                                            step=step,
                                            summary=request.task[:MAX_PROGRESS_CHARS],
                                            metadata=_subtask_request_metadata(request),
                                        )
                                    for request in subtask_requests:
                                        yield self._event(
                                            "subtask.started",
                                            step=step,
                                            summary=request.task[:MAX_PROGRESS_CHARS],
                                            metadata=_subtask_request_metadata(request),
                                        )
                                    try:
                                        result, subtask_results = (
                                            await registered_tool.execute_batch(
                                                call, subtask_requests
                                            )
                                        )
                                    except asyncio.CancelledError:
                                        self.cancel_event.set()
                                        for request in subtask_requests:
                                            yield self._event(
                                                "subtask.cancelled",
                                                step=step,
                                                summary=request.task[:MAX_PROGRESS_CHARS],
                                                metadata={
                                                    **_subtask_request_metadata(request),
                                                    "status": "cancelled",
                                                },
                                            )
                                        self.state = transition(
                                            self.state, RunState.CANCELLED
                                        )
                                        for event in self._cancel_pending_tool_calls(
                                            calls,
                                            call_index,
                                            step,
                                            current_requested=True,
                                        ):
                                            yield event
                                        yield self._event(
                                            "run.cancelled",
                                            step=step,
                                            summary=_cancelled_summary(self.language),
                                            metadata=self._terminal_metadata(
                                                incomplete_reason="RUN_CANCELLED"
                                            ),
                                        )
                                        return
                                    for subtask_result in subtask_results:
                                        event_type: RuntimeEventType = (
                                            "subtask.completed"
                                            if subtask_result.status
                                            is SubtaskStatus.COMPLETED
                                            else "subtask.failed"
                                        )
                                        yield self._event(
                                            event_type,
                                            step=step,
                                            summary=subtask_result.summary,
                                            metadata=subtask_result.public_metadata(),
                                            error_code=subtask_result.error_code,
                                            error_message=(
                                                subtask_result.summary
                                                if subtask_result.error_code
                                                else None
                                            ),
                                        )
                            elif self.tools.approval_required(call):
                                if self.approvals is None:
                                    result = _runtime_tool_error(
                                        call,
                                        "APPROVAL_UNAVAILABLE",
                                        "Interactive approval is unavailable",
                                    )
                                else:
                                    prepared = self._tool_dispatcher.prepare_approval(call)
                                    if isinstance(prepared, ToolResult):
                                        result = prepared
                                    else:
                                        request = self.approvals.request(
                                            run_id=self.run_id,
                                            session_id=self.session_id,
                                            tool_call_id=call.id,
                                            tool_name=call.name,
                                            summary=_approval_summary(call, self.language),
                                            parameters=prepared.parameters,
                                            preview=prepared.preview,
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
                                            for event in self._cancel_pending_tool_calls(
                                                calls,
                                                call_index,
                                                step,
                                                current_requested=True,
                                            ):
                                                yield event
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
                                            try:
                                                result = await self._tool_dispatcher.execute(
                                                    call,
                                                    approved=True,
                                                    expected_hashes=prepared.expected_hashes,
                                                )
                                            except asyncio.CancelledError:
                                                self.state = transition(
                                                    self.state, RunState.CANCELLED
                                                )
                                                for event in self._cancel_pending_tool_calls(
                                                    calls,
                                                    call_index,
                                                    step,
                                                    current_requested=True,
                                                ):
                                                    yield event
                                                yield self._event(
                                                    "run.cancelled",
                                                    step=step,
                                                    summary=_cancelled_summary(self.language),
                                                    metadata=self._terminal_metadata(
                                                        incomplete_reason="RUN_CANCELLED"
                                                    ),
                                                )
                                                return
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
                                try:
                                    result = await self._tool_dispatcher.execute(call)
                                except asyncio.CancelledError:
                                    self.state = transition(self.state, RunState.CANCELLED)
                                    for event in self._cancel_pending_tool_calls(
                                        calls,
                                        call_index,
                                        step,
                                        current_requested=True,
                                    ):
                                        yield event
                                    yield self._event(
                                        "run.cancelled",
                                        step=step,
                                        summary=_cancelled_summary(self.language),
                                        metadata=self._terminal_metadata(
                                            incomplete_reason="RUN_CANCELLED"
                                        ),
                                    )
                                    return
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
                    if (
                        call.name == "git_inspect"
                        and result.error is not None
                        and result.error.code == "GIT_NOT_REPOSITORY"
                    ):
                        self._disabled_tools.add(call.name)
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
                        terminal_code = failure.terminal_error_code
                        self.state = transition(self.state, RunState.FAILED)
                        yield self._event(
                            "run.failed",
                            step=step,
                            error_code=terminal_code,
                            error_message=failure.terminal_message,
                            metadata=self._terminal_metadata(
                                incomplete_reason=(
                                    failure.incomplete_reason
                                    or terminal_code
                                ),
                                **(
                                    {"primary_error_code": self.evidence.primary_error_code}
                                    if self.evidence.primary_error_code
                                    else {}
                                ),
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
                decision = self.evidence.validate_completed(language=self.language)
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

        if self.max_steps is None:
            return
        # An explicit max_steps is an emergency fuse. Exhaustion still needs a
        # terminal event, while an unbounded run relies on model completion,
        # cancellation, and the existing repetition safeguards.
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
        **extra: object,
    ) -> dict[str, object]:
        return {
            "evidence": self.evidence.public_summary(),
            "metrics": self.metrics.to_dict(),
            "context": self.metrics.context_dict(),
            **extra,
            **({"incomplete_reason": incomplete_reason} if incomplete_reason else {}),
        }

    async def _run_delegated_analysis(
        self, request: SubtaskRequest
    ) -> SubtaskResult:
        """Run one isolated child loop and retain only bounded public evidence."""

        started = time.monotonic()
        observed_files: set[str] = set()
        checks: list[str] = []
        terminal_type = "run.failed"
        terminal_answer = ""
        terminal_error = "SUBTASK_FAILED"

        def scoped_factory(path: Path, **_: object) -> ToolRegistry:
            return ScopedReadOnlyToolRegistry(path, request.scope)

        child = CodingAgent(
            self.workspace,
            self.model_client,
            max_steps=CHILD_MAX_STEPS,
            context_chars=64_000,
            run_id=f"{self.run_id}:subtask:{request.id}",
            cancel_event=self.cancel_event,
            tool_registry_factory=scoped_factory,
            language=self.language,
            permission_mode=PermissionMode.INSPECT,
            session_id=self.session_id,
            history=(),
            history_sources=(),
            # Workers have a fixed public JSON completion contract. Their last
            # bounded step exposes only report_run_outcome (see
            # summary_outcome_only), forcing a protocol-valid structured result.
            reserve_summary_round=True,
            include_outcome_tool=True,
            context_sanitizer=self._context_sanitizer,
            enable_delegation=False,
            system_message=worker_system_prompt(self.language, request.scope),
            model_output_tokens=4_096,
            summary_outcome_only=True,
        )
        async for event in child.run(request.task):
            if (
                event.type == "tool.completed"
                and event.metadata
                and event.metadata.get("ok") is True
                and event.tool
            ):
                check = event.tool
                operation = event.metadata.get("operation")
                if isinstance(operation, str):
                    check = f"{check}:{operation}"
                if check not in checks:
                    checks.append(check)
                path = event.metadata.get("path")
                if isinstance(path, str):
                    observed_files.add(path)
            if event.type in {
                "run.completed",
                "run.incomplete",
                "run.failed",
                "run.cancelled",
            }:
                terminal_type = event.type
                terminal_answer = event.answer or ""
                terminal_error = event.error_code or terminal_type.upper().replace(".", "_")

        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        usage = SubtaskUsage(
            input_tokens=child.metrics.prompt_tokens,
            output_tokens=child.metrics.completion_tokens,
        )
        if terminal_type == "run.cancelled":
            raise asyncio.CancelledError

        status = (
            SubtaskStatus.COMPLETED
            if terminal_type == "run.completed"
            else SubtaskStatus.INCOMPLETE
            if terminal_type == "run.incomplete"
            else SubtaskStatus.FAILED
        )
        if status is SubtaskStatus.COMPLETED:
            try:
                summary, facts, blockers = parse_worker_answer(
                    terminal_answer,
                    sanitizer=self._context_sanitizer,
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                summary = (
                    "子任务未返回有效的结构化证据"
                    if self.language == "zh"
                    else "The worker did not return valid structured evidence"
                )
                facts = ()
                blockers = ("SUBTASK_RESULT_INVALID",)
                status = SubtaskStatus.INCOMPLETE
                terminal_error = "SUBTASK_RESULT_INVALID"
        else:
            # An incomplete worker still has useful, runtime-observed evidence
            # such as files and tools used. Preserve that boundary explicitly;
            # do not reinterpret an exhausted worker as malformed JSON.
            summary = (
                "子任务在有限步数内完成了部分观察，但未形成结构化结论"  # noqa: RUF001
                if self.language == "zh"
                else (
                    "The worker gathered partial observations but did not finish "
                    "its structured conclusion"
                )
            )
            facts = ()
            blockers = (
                "SUBTASK_BUDGET_EXHAUSTED"
                if status is SubtaskStatus.INCOMPLETE
                else "SUBTASK_FAILED",
            )
            terminal_error = (
                "SUBTASK_BUDGET_EXHAUSTED"
                if status is SubtaskStatus.INCOMPLETE
                else terminal_error
            )

        return SubtaskResult(
            request=request,
            status=status,
            summary=summary,
            facts=facts,
            files_observed=tuple(sorted(observed_files)),
            checks=tuple(checks),
            blockers=blockers,
            usage=usage,
            duration_ms=duration_ms,
            error_code=(None if status is SubtaskStatus.COMPLETED else terminal_error),
        )

    def _model_request(
        self, messages: tuple[ModelMessage, ...], summary_round: bool
    ) -> ModelRequest:
        available_definitions = tuple(
            definition
            for definition in self.tools.definitions
            if definition.name not in self._disabled_tools
        )
        if summary_round and self._summary_outcome_only:
            return ModelRequest(
                messages=messages,
                max_tokens=self._model_output_tokens,
                tools=(OUTCOME_TOOL,),
                tool_choice="auto",
            )
        return ModelRequest(
            messages=messages,
            max_tokens=self._model_output_tokens,
            tools=(
                ()
                if summary_round
                else (
                    (
                        *available_definitions,
                        # Definitions for tools disabled by observed workspace
                        # facts are intentionally omitted on later requests.
                        OUTCOME_TOOL,
                        *((REQUEST_USER_INPUT_TOOL,) if self.user_inputs else ()),
                    )
                    if self.include_outcome_tool
                    else available_definitions
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

    def _cancel_pending_tool_calls(
        self,
        calls: tuple[ToolCall, ...],
        start_index: int,
        step: int,
        *,
        current_requested: bool,
    ) -> tuple[RuntimeEvent, ...]:
        """Close every outstanding assistant call with a safe tool result."""

        events: list[RuntimeEvent] = []
        first_index = start_index if current_requested else start_index + 1
        for call in calls[first_index:]:
            if call is not calls[start_index] or not current_requested:
                events.append(
                    self._event(
                        "tool.requested",
                        step=step,
                        tool=call.name,
                        metadata={"tool_call_id": call.id},
                    )
                )
            result = tool_error_result(
                call,
                "TOOL_CANCELLED",
                "Tool call was cancelled before completion",
            )
            self.evidence.record(call.name, result)
            self.context.append(
                ModelMessage(role="tool", content=result.to_model_content(), tool_call_id=call.id)
            )
            events.append(
                self._event(
                    "tool.completed",
                    step=step,
                    tool=call.name,
                    metadata={
                        **result.public_metadata,
                        "tool_call_id": call.id,
                        "ok": False,
                    },
                )
            )
        return tuple(events)

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


def _subtask_request_metadata(request: SubtaskRequest) -> dict[str, object]:
    return {
        "subtask_id": request.id,
        "subtask_index": request.index,
        "scope": list(request.scope),
        "expected_output": request.expected_output.value,
    }


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


def _disabled_tool_message(tool: str, language: str) -> str:
    if language == "zh":
        return f"{tool} 已根据工作区事实标记为不可用\uFF1B请使用其他可用工具继续。"
    return (
        f"{tool} was disabled by an observed workspace fact; "
        "continue with another available tool."
    )


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
    return tool_error_result(
        call,
        code,
        message,
        recoverable=recoverable,
    )
