"""Execute a confirmed plan as ordered tasks inside one coding runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from leanharness.context import ContextSource
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.planning.contracts import Plan, PlanStep, PlanStepState
from leanharness.runtime import CodingAgent, RuntimeEvent
from leanharness.runtime.loop import RuntimeModelClient
from leanharness.runtime.metrics import RunMetrics
from leanharness.tools import ToolRegistry

PlanEventType = str
StepUpdater = Callable[[str, PlanStepState, dict[str, object] | None, str | None], None]

MAX_STEP_ANSWER_CHARS = 6_000
MAX_PLAN_CONTEXT_CHARS = 12_000

# A plan can be confirmed while the session is still in inspect mode.  If the
# model then asks for a capability that the current registry intentionally does
# not expose, that is a recoverable permission boundary, not a broken plan or a
# broken model request.  Keep this mapping narrow so real runtime failures still
# surface as plan.failed.
_RECOVERABLE_PERMISSION_CODES = frozenset(
    {
        "TOOL_NOT_FOUND",
        "TOOL_UNAVAILABLE",
        "MUTATION_NOT_REQUESTED",
        "APPROVAL_UNAVAILABLE",
    }
)


@dataclass(frozen=True, slots=True)
class PlanEvent:
    type: PlanEventType
    sequence: int
    run_id: str
    plan_id: str
    step: int | None = None
    summary: str | None = None
    answer: str | None = None
    metadata: dict[str, object] | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
        }
        for key in ("step", "summary", "answer", "metadata"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.error_code:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message or "Plan failed",
            }
        return payload


class PlanController:
    def __init__(
        self,
        plan: Plan,
        workspace: Path,
        model_client: RuntimeModelClient,
        *,
        permission_mode: PermissionMode,
        language: str,
        max_steps: int | None = None,
        approvals: ApprovalCoordinator | None = None,
        tool_registry_factory: Callable[[Path], ToolRegistry] = ToolRegistry,
        initial_sequence: int = 0,
        on_step: StepUpdater | None = None,
        cancel_event: asyncio.Event | None = None,
        history_sources: tuple[ContextSource, ...] = (),
        initial_metrics: RunMetrics | None = None,
    ) -> None:
        if not plan.run_id:
            raise ValueError("Confirmed plan must be attached to a run")
        self.plan = plan
        self._sequence = initial_sequence
        self._on_step = on_step
        # ``None`` is the normal Plan Mode setting.  The agent owns the loop
        # and decides when the task is complete; an integer is only an
        # explicit emergency fuse retained for callers that need one.
        self.max_steps = max_steps
        self._remaining_budget = max_steps
        self._step_answers: list[tuple[str, str]] = []
        self._step_evidence: list[dict[str, object]] = [
            step.evidence
            for step in plan.steps
            if step.state is PlanStepState.COMPLETED and isinstance(step.evidence, dict)
        ]
        self._completed_step_ids: list[str] = [
            step.id for step in plan.steps if step.state is PlanStepState.COMPLETED
        ]
        self.agent = CodingAgent(
            workspace,
            model_client,
            max_steps=max_steps,
            run_id=plan.run_id,
            language=language,
            permission_mode=permission_mode,
            session_id=plan.session_id,
            approvals=approvals,
            tool_registry_factory=tool_registry_factory,
            initial_sequence=initial_sequence,
            cancel_event=cancel_event,
            reserve_summary_round=False,
            history_sources=history_sources,
            metrics=initial_metrics,
        )

    async def run(self) -> AsyncIterator[RuntimeEvent | PlanEvent]:
        enabled = tuple(
            step
            for step in self.plan.steps
            if step.enabled and step.state is not PlanStepState.COMPLETED
        )
        if not enabled:
            yield self._event("plan.completed", summary="Plan already completed")
            yield self._runtime_event(
                "run.completed",
                answer=self._build_report("completed"),
                summary="Plan completed",
                metadata=self._terminal_metadata(),
            )
            return
        if self.max_steps is None:
            async for event in self._run_shared_loop(enabled):
                yield event
            return
        last_answer: str | None = None
        completed_titles = [
            step.title for step in self.plan.steps if step.state is PlanStepState.COMPLETED
        ]
        for index, step in enumerate(enabled):
            if self._remaining_budget < 1:
                answer = self._build_report("paused")
                yield self._event(
                    "plan.paused",
                    summary="Plan model budget exhausted before the next step",
                    answer=answer or None,
                    metadata={"reason": "PLAN_BUDGET_EXHAUSTED"},
                )
                yield self._runtime_event(
                    "run.incomplete",
                    answer=answer or None,
                    summary="Plan model budget exhausted",
                    metadata=self._terminal_metadata(
                        incomplete_reason="PLAN_BUDGET_EXHAUSTED"
                    ),
                )
                return
            self._update_step(step, PlanStepState.RUNNING)
            yield self._event(
                "plan.step.started",
                step=step.sequence,
                summary=step.title,
                metadata={
                    "step_id": step.id,
                    "instruction": step.instruction,
                    "permission_mode": self.agent.permission_mode.value,
                },
            )
            task = _step_task(self.plan, step, completed_titles)
            self.agent.max_steps = self._remaining_budget
            self.agent.set_event_sequence(self._sequence)
            stream = (
                self.agent.run(task)
                if index == 0
                else self.agent.continue_task(
                    task,
                )
            )
            calls_before = self.agent.metrics.model_calls
            terminal: RuntimeEvent | None = None
            async for event in stream:
                if event.type in {
                    "run.started",
                    "run.completed",
                    "run.incomplete",
                    "run.failed",
                    "run.cancelled",
                }:
                    if event.type != "run.started":
                        terminal = event
                    continue
                # Runtime sequences are private to the shared agent. Plan events use
                # one contiguous public sequence and carry the outer plan-step scope.
                yield self._annotate_runtime_event(event, step)
            self._remaining_budget = max(
                0, self._remaining_budget - (self.agent.metrics.model_calls - calls_before)
            )
            if terminal is None:
                self._update_step(step, PlanStepState.FAILED, error_code="PLAN_STEP_INTERRUPTED")
                yield self._event(
                    "plan.failed",
                    step=step.sequence,
                    error_code="PLAN_STEP_INTERRUPTED",
                    error_message="Plan step ended without a terminal event",
                )
                yield self._runtime_event(
                    "run.failed",
                    error_code="PLAN_STEP_INTERRUPTED",
                    error_message="Plan step ended without a terminal event",
                )
                return
            if terminal.type == "run.completed":
                evidence = _terminal_evidence(terminal)
                answer = _bounded_text(terminal.answer or "", MAX_STEP_ANSWER_CHARS)
                self._update_step(step, PlanStepState.COMPLETED, evidence=evidence)
                self._step_evidence.append(evidence)
                self._completed_step_ids.append(step.id)
                if answer:
                    self._step_answers.append((step.title, answer))
                    last_answer = answer
                completed_titles.append(step.title)
                yield self._event(
                    "plan.step.completed",
                    step=step.sequence,
                    summary=step.title,
                    metadata={
                        "step_id": step.id,
                        "evidence": evidence,
                        **_verification_metadata(evidence),
                        **({"answer": answer} if answer else {}),
                    },
                )
                if index + 1 < len(enabled):
                    self.agent.checkpoint_context(
                        _step_context(self.plan, self._step_answers)
                    )
                continue
            if terminal.type == "run.incomplete":
                self._update_step(
                    step,
                    PlanStepState.FAILED,
                    evidence=_terminal_evidence(terminal),
                    error_code="PLAN_STEP_INCOMPLETE",
                )
                yield self._event(
                    "plan.paused",
                    step=step.sequence,
                    summary=terminal.summary,
                    answer=terminal.answer,
                    metadata={"step_id": step.id},
                )
                yield self._annotate_runtime_event(terminal, step)
                return
            failed_state = (
                PlanStepState.FAILED
                if terminal.type == "run.failed"
                else PlanStepState.PENDING
            )
            self._update_step(step, failed_state, error_code=terminal.error_code)
            event_type = "plan.failed" if terminal.type == "run.failed" else "plan.cancelled"
            yield self._event(
                event_type,
                step=step.sequence,
                summary=terminal.summary,
                error_code=terminal.error_code,
                error_message=terminal.error_message,
            )
            yield self._annotate_runtime_event(terminal, step)
            return
        yield self._event("plan.completed", summary="All plan steps completed")
        yield self._runtime_event(
            "run.completed",
            answer=(
                self._build_report("completed")
                or last_answer
                or "All enabled plan steps are complete."
            ),
            summary="Plan completed",
            metadata=self._terminal_metadata(),
        )

    async def _run_shared_loop(
        self, enabled: tuple[PlanStep, ...]
    ) -> AsyncIterator[RuntimeEvent | PlanEvent]:
        """Run the whole plan through one model loop.

        Plan steps remain visible audit records, but they do not become hidden
        sub-runs.  The model receives the plan as one task and the runtime only
        records the plan-level terminal result.  This keeps decision ownership
        in ``CodingAgent`` and avoids arbitrary per-step budget boundaries.
        """
        for step in enabled:
            self._update_step(step, PlanStepState.RUNNING)
        yield self._event(
            "plan.step.started",
            summary="Plan execution started",
            metadata={
                "step_ids": [step.id for step in enabled],
                "permission_mode": self.agent.permission_mode.value,
                "shared_runtime": True,
            },
        )
        task = _shared_plan_task(self.plan, enabled)
        self.agent.max_steps = None
        self.agent.set_event_sequence(self._sequence)
        terminal: RuntimeEvent | None = None
        async for event in self.agent.run(task):
            if event.type in {
                "run.started",
                "run.completed",
                "run.incomplete",
                "run.failed",
                "run.cancelled",
            }:
                if event.type != "run.started":
                    terminal = event
                continue
            yield self._annotate_shared_runtime_event(event)
        if terminal is None:
            for step in enabled:
                self._update_step(
                    step,
                    PlanStepState.FAILED,
                    error_code="PLAN_STEP_INTERRUPTED",
                )
            yield self._event(
                "plan.failed",
                error_code="PLAN_STEP_INTERRUPTED",
                error_message="Plan ended without a terminal event",
            )
            yield self._runtime_event(
                "run.failed",
                error_code="PLAN_STEP_INTERRUPTED",
                error_message="Plan ended without a terminal event",
            )
            return
        if terminal.type == "run.completed":
            evidence = _terminal_evidence(terminal)
            # Completion is a single loop decision.  Marking the enabled plan
            # records together reflects that fact; evidence is deliberately
            # shared instead of pretending each step had an independent run.
            for step in enabled:
                self._update_step(step, PlanStepState.COMPLETED, evidence=evidence)
                self._completed_step_ids.append(step.id)
            self._step_evidence.append(evidence)
            yield self._event(
                "plan.step.completed",
                summary="Plan completed by shared agent loop",
                metadata={
                    "step_ids": [step.id for step in enabled],
                    "evidence": evidence,
                    "shared_runtime": True,
                },
            )
            yield self._event("plan.completed", summary="All plan steps completed")
            yield self._runtime_event(
                "run.completed",
                answer=self._build_report("completed"),
                summary="Plan completed",
                metadata=self._terminal_metadata(),
            )
            return
        terminal_step_state = (
            PlanStepState.FAILED
            if terminal.type == "run.failed"
            else PlanStepState.PENDING
        )

        permission_reason = _permission_pause_reason(terminal)
        if permission_reason is not None:
            for step in enabled:
                self._update_step(step, PlanStepState.PENDING, error_code=permission_reason)
            report = self._build_report("paused")
            yield self._event(
                "plan.paused",
                summary=_permission_pause_summary(permission_reason, self.agent.language),
                answer=report,
                metadata={
                    "step_ids": [step.id for step in enabled],
                    "reason": permission_reason,
                    "requires_permission_change": True,
                },
            )
            yield self._runtime_event(
                "run.incomplete",
                answer=report,
                summary=_permission_pause_summary(permission_reason, self.agent.language),
                error_code=permission_reason,
                metadata=self._terminal_metadata(
                    incomplete_reason="PERMISSION_REQUIRED",
                    permission_required=True,
                ),
            )
            return
        for step in enabled:
            self._update_step(
                step,
                terminal_step_state,
                evidence=_terminal_evidence(terminal),
                error_code=terminal.error_code,
            )
        if terminal.type == "run.incomplete":
            report = self._build_report("paused")
            reason = _incomplete_reason(terminal)
            summary = _plan_incomplete_summary(reason, terminal.summary, self.agent.language)
            yield self._event(
                "plan.paused",
                summary=summary,
                answer=report,
                metadata={
                    "step_ids": [step.id for step in enabled],
                    "reason": reason,
                    "requires_permission_change": reason == "PERMISSION_REQUIRED",
                },
            )
        else:
            yield self._event(
                "plan.failed" if terminal.type == "run.failed" else "plan.cancelled",
                summary=terminal.summary,
                error_code=terminal.error_code,
                error_message=terminal.error_message,
            )
        if terminal.type == "run.incomplete":
            terminal = replace(
                terminal,
                answer=report,
                summary=summary,
                error_code=(
                    "TOOL_NOT_FOUND"
                    if reason == "PERMISSION_REQUIRED"
                    else terminal.error_code
                ),
                metadata={
                    **(terminal.metadata or {}),
                    "incomplete_reason": reason,
                    "permission_required": reason == "PERMISSION_REQUIRED",
                },
            )
        yield self._annotate_shared_runtime_event(terminal)

    def _annotate_runtime_event(self, event: RuntimeEvent, step: PlanStep) -> RuntimeEvent:
        metadata = dict(event.metadata or {})
        metadata.update({"plan_step": step.sequence, "plan_step_id": step.id})
        return replace(event, sequence=self._next_sequence(), metadata=metadata)

    def _annotate_shared_runtime_event(self, event: RuntimeEvent) -> RuntimeEvent:
        metadata = dict(event.metadata or {})
        metadata.update({"plan_id": self.plan.id, "shared_runtime": True})
        return replace(event, sequence=self._next_sequence(), metadata=metadata)

    def _build_report(self, status: str) -> str:
        """Render a bounded, line-complete report from public runtime facts."""
        completed = set(self._completed_step_ids)
        chinese = self.agent.language == "zh"
        if chinese:
            lines = ["# 计划报告", "", f"状态: {_report_status(status, chinese)}", ""]
            lines.append("## 步骤")
        else:
            lines = ["# Plan report", "", f"Status: {status}", ""]
            lines.append("## Steps")
        for step in sorted(self.plan.steps, key=lambda item: item.sequence):
            state = "COMPLETED" if step.id in completed else step.state.value
            lines.append(f"\n## {step.title}")
            if chinese:
                lines.append(f"- 序号: {step.sequence}")
                lines.append(f"- 状态: {_report_step_state(state)}")
            else:
                lines.append(f"- Sequence: {step.sequence}")
                lines.append(f"- Status: {state}")
        evidence = _aggregate_evidence(self._step_evidence)
        changed = evidence.get("changed_files", [])
        lines.extend(["", "## 证据" if chinese else "## Evidence"])
        if chinese:
            lines.append(
                "- 观察: "
                f"{evidence.get('observations', 0)}; 修改: {evidence.get('mutations', 0)}; "
                f"验证: {evidence.get('verifications', 0)}"
            )
        else:
            lines.append(
                "- Observations: "
                f"{evidence.get('observations', 0)}; mutations: {evidence.get('mutations', 0)}; "
                f"verifications: {evidence.get('verifications', 0)}"
            )
        if changed:
            lines.append(
                ("- 修改文件: " if chinese else "- Changed files: ")
                + ", ".join(str(path) for path in changed)
            )
        unresolved = evidence.get("unresolved_errors", [])
        if unresolved:
            lines.append(
                ("- 未解决错误: " if chinese else "- Unresolved errors: ")
                + ", ".join(str(code) for code in unresolved)
            )
        denials = evidence.get("verification_argument_denials", 0)
        recoveries = evidence.get("verification_recoveries", 0)
        if denials:
            lines.append(
                (
                    f"- 验证偏差: {denials} 个命令被拒绝, "
                    if chinese
                    else f"- Verification deviations: {denials} denied command(s), "
                )
                + (
                    f"{recoveries} 个已恢复"
                    if chinese
                    else f"{recoveries} recovered"
                )
            )
        failures = evidence.get("verification_failures", [])
        if failures:
            rendered_failures = ", ".join(
                str(item.get("code"))
                + (
                    f" ({item['profile']})"
                    if isinstance(item, dict) and item.get("profile")
                    else ""
                )
                for item in failures
                if isinstance(item, dict)
            )
            if rendered_failures:
                lines.append(
                    ("- 验证失败: " if chinese else "- Verification failures: ")
                    + rendered_failures
                )
        profiles = evidence.get("verification_profiles", [])
        if profiles:
            lines.append(
                ("- 成功验证配置: " if chinese else "- Successful verification profiles: ")
                + ", ".join(map(str, profiles))
            )
        if status != "completed":
            lines.extend(
                [
                    "",
                    "## 待处理" if chinese else "## Remaining",
                    "- 计划尚未完成, 请恢复或修改。"
                    if chinese
                    else "- The plan is not complete; resume or revise it.",
                ]
            )
        return "\n".join(lines)

    def _terminal_metadata(
        self,
        *,
        incomplete_reason: str | None = None,
        permission_required: bool = False,
    ) -> dict[str, object]:
        return {
            "plan_id": self.plan.id,
            "completed_step_ids": list(self._completed_step_ids),
            "evidence": _aggregate_evidence(self._step_evidence),
            "metrics": self.agent.metrics.to_dict(),
            "context": self.agent.metrics.context_dict(),
            **({"incomplete_reason": incomplete_reason} if incomplete_reason else {}),
            **({"permission_required": True} if permission_required else {}),
        }

    def _update_step(
        self,
        step: PlanStep,
        state: PlanStepState,
        evidence: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._on_step:
            self._on_step(step.id, state, evidence, error_code)

    def _event(self, event_type: str, **kwargs: Any) -> PlanEvent:
        return PlanEvent(
            type=event_type,
            sequence=self._next_sequence(),
            run_id=self.plan.run_id or "",
            plan_id=self.plan.id,
            **kwargs,
        )

    def _runtime_event(self, event_type: str, **kwargs: Any) -> RuntimeEvent:
        return RuntimeEvent(
            type=event_type,  # type: ignore[arg-type]
            sequence=self._next_sequence(),
            run_id=self.plan.run_id or "",
            **kwargs,
        )

    def _next_sequence(self) -> int:
        value = self._sequence
        self._sequence += 1
        return value


def _step_task(plan: Plan, step: PlanStep, completed: list[str]) -> str:
    return (
        f"Execute plan step {step.sequence} of {len(plan.steps)}. "
        f"Plan goal: {plan.task}\nCurrent step: {step.title}\n"
        f"Instruction: {step.instruction}\nCompleted steps: {completed}. "
        "Work only on this step. Obtain the evidence required for this step and then report "
        "its result, changed files, verification, and any remaining blocker."
    )


def _shared_plan_task(plan: Plan, steps: tuple[PlanStep, ...]) -> str:
    rendered = "\n".join(
        f"{step.sequence}. {step.title}: {step.instruction}" for step in steps
    )
    return (
        "Work on the following implementation plan as one continuous coding task. "
        "The plan is context, not a separate decision process: choose the next action "
        "with the available tools, verify the workspace, and report only when the whole "
        "request is complete. Do not claim work that has no tool evidence.\n"
        f"Plan goal: {plan.task}\nPlan steps:\n{rendered}"
    )


def _permission_pause_reason(event: RuntimeEvent) -> str | None:
    """Return a recoverable permission code carried by a failed run."""

    if event.type not in {"run.failed", "run.incomplete"}:
        return None
    candidates: list[object] = [event.error_code]
    metadata = event.metadata or {}
    candidates.append(metadata.get("primary_error_code"))
    evidence = metadata.get("evidence")
    if isinstance(evidence, dict):
        candidates.extend(evidence.get("unresolved_errors", []))
    for value in candidates:
        if isinstance(value, str) and value in _RECOVERABLE_PERMISSION_CODES:
            return value
    # A repeated unavailable call is normally classified as RUN_STALLED by
    # the runtime.  The causal error is still preserved in the evidence; if
    # it is present, pausing is safer and recoverable than marking the plan
    # permanently failed.
    if event.type == "run.failed" and event.error_code == "RUN_STALLED":
        evidence = metadata.get("evidence")
        if isinstance(evidence, dict):
            for code in evidence.get("unresolved_errors", []):
                if isinstance(code, str) and code in _RECOVERABLE_PERMISSION_CODES:
                    return code
    # Repeated requests for an unavailable tool are surfaced by the runtime as
    # RUN_STALLED with the original code retained in primary_error_code/evidence.
    return None


def _permission_pause_summary(code: str, language: str) -> str:
    if language == "zh":
        return f"当前权限不允许执行所需操作（{code}），切换会话权限后可恢复计划"  # noqa: RUF001
    return (
        "The current permission does not allow the required action "
        f"({code}); change the session permission and resume the plan"
    )


def _incomplete_reason(event: RuntimeEvent) -> str:
    metadata = event.metadata or {}
    value = metadata.get("incomplete_reason")
    if isinstance(value, str) and value:
        if value == "MODEL_REPORTED_INCOMPLETE":
            evidence = metadata.get("evidence")
            if isinstance(evidence, dict):
                for code in evidence.get("unresolved_errors", []):
                    if isinstance(code, str) and code in _RECOVERABLE_PERMISSION_CODES:
                        return "PERMISSION_REQUIRED"
        return value
    return "MODEL_REPORTED_INCOMPLETE"


def _plan_incomplete_summary(reason: str, fallback: str | None, language: str) -> str:
    if reason == "PERMISSION_REQUIRED":
        return _permission_pause_summary("TOOL_NOT_FOUND", language)
    if reason in _RECOVERABLE_PERMISSION_CODES:
        return _permission_pause_summary(reason, language)
    if reason == "MODEL_REPORTED_INCOMPLETE":
        return (
            "Agent 报告当前任务尚未完成，计划已暂停，可恢复继续执行"  # noqa: RUF001
            if language == "zh"
            else (
                "The agent reported that the task is not complete; "
                "the plan is paused and can be resumed"
            )
        )
    if reason in {"STEP_BUDGET_EXHAUSTED", "PLAN_BUDGET_EXHAUSTED"}:
        return (
            "运行预算已用完，计划已暂停"  # noqa: RUF001
            if language == "zh"
            else "The run budget was exhausted; the plan is paused"
        )
    return fallback or (
        "计划已暂停，尚未完成"  # noqa: RUF001
        if language == "zh"
        else "The plan is paused before completion"
    )


def _report_status(status: str, chinese: bool) -> str:
    if not chinese:
        return status
    return {"completed": "已完成", "paused": "已暂停"}.get(status, status)


def _report_step_state(state: str) -> str:
    return {
        "COMPLETED": "已完成",
        "RUNNING": "执行中",
        "FAILED": "失败",
        "SKIPPED": "已跳过",
        "PENDING": "待处理",
    }.get(state, state)


def _step_context(plan: Plan, answers: list[tuple[str, str]]) -> str:
    details = "\n\n".join(
        f"### {title}\n{answer}" for title, answer in answers[-8:]
    )
    return _bounded_text(
        "Bounded evidence from completed plan steps. Use it as a summary, and re-read "
        "the workspace when exact details are needed.\n"
        f"Plan goal: {plan.task}\n{details}",
        MAX_PLAN_CONTEXT_CHARS,
    )


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _terminal_evidence(event: RuntimeEvent) -> dict[str, object]:
    metadata = event.metadata or {}
    evidence = metadata.get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def _aggregate_evidence(items: list[dict[str, object]]) -> dict[str, object]:
    count_keys = (
        "observations",
        "mutation_attempts",
        "mutations",
        "verification_attempts",
        "verifications",
    )
    counts = {
        key: sum(
            value
            for item in items
            if isinstance((value := item.get(key)), int)
        )
        for key in count_keys
    }
    verification_argument_denials = sum(
        value
        for item in items
        if isinstance((value := item.get("verification_argument_denials")), int)
    )
    verification_recoveries = sum(
        value
        for item in items
        if isinstance((value := item.get("verification_recoveries")), int)
    )
    verification_failures = sorted(
        {
            (str(item.get("code")), str(item.get("profile")))
            for evidence in items
            for item in evidence.get("verification_failures", [])
            if isinstance(item, dict) and item.get("code")
        }
    )
    verification_profiles = sorted(
        {
            str(profile)
            for evidence in items
            for profile in evidence.get("verification_profiles", [])
            if isinstance(profile, str)
        }
    )
    changed_files = sorted(
        {
            str(path)
            for item in items
            for path in item.get("changed_files", [])
            if isinstance(path, str)
        }
    )
    unresolved_errors = list(
        dict.fromkeys(
            str(code)
            for item in items
            for code in item.get("unresolved_errors", [])
            if isinstance(code, str)
        )
    )
    return {
        **counts,
        "changed_files": changed_files,
        "unresolved_errors": unresolved_errors,
        "verification_argument_denials": verification_argument_denials,
        "verification_recoveries": verification_recoveries,
        "verification_failures": [
            {"code": code, **({"profile": profile} if profile != "None" else {})}
            for code, profile in verification_failures
        ],
        "verification_profiles": verification_profiles,
    }


def _verification_metadata(evidence: dict[str, object]) -> dict[str, object]:
    denials = evidence.get("verification_argument_denials")
    recoveries = evidence.get("verification_recoveries")
    failures = evidence.get("verification_failures")
    if isinstance(denials, int) and denials > 0:
        return {
            "verification_note": (
                "The requested command arguments were denied; a permitted verification "
                f"command later succeeded ({recoveries or 0} recovery)."
            )
        }
    if isinstance(failures, list) and failures:
        return {"verification_note": "One or more verification commands failed."}
    return {}
