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
MAX_PLAN_ANSWER_CHARS = 32_000
MAX_PLAN_CONTEXT_CHARS = 12_000


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
        max_steps: int = 24,
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
                answer="All enabled plan steps are complete.",
                summary="Plan completed",
                metadata=self._terminal_metadata(),
            )
            return
        last_answer: str | None = None
        completed_titles = [
            step.title for step in self.plan.steps if step.state is PlanStepState.COMPLETED
        ]
        for index, step in enumerate(enabled):
            if self._remaining_budget < 1:
                answer = self._combined_answer()
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
            answer=self._combined_answer() or last_answer or "All enabled plan steps are complete.",
            summary="Plan completed",
            metadata=self._terminal_metadata(),
        )

    def _annotate_runtime_event(self, event: RuntimeEvent, step: PlanStep) -> RuntimeEvent:
        metadata = dict(event.metadata or {})
        metadata.update({"plan_step": step.sequence, "plan_step_id": step.id})
        return replace(event, sequence=self._next_sequence(), metadata=metadata)

    def _combined_answer(self) -> str:
        if not self._step_answers:
            return ""
        rendered = "\n\n".join(
            f"## {title}\n{answer}" for title, answer in self._step_answers
        )
        return _bounded_text(rendered, MAX_PLAN_ANSWER_CHARS)

    def _terminal_metadata(
        self,
        *,
        incomplete_reason: str | None = None,
    ) -> dict[str, object]:
        return {
            "plan_id": self.plan.id,
            "completed_step_ids": list(self._completed_step_ids),
            "evidence": _aggregate_evidence(self._step_evidence),
            "metrics": self.agent.metrics.to_dict(),
            "context": self.agent.metrics.context_dict(),
            **({"incomplete_reason": incomplete_reason} if incomplete_reason else {}),
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
    }


def _verification_metadata(evidence: dict[str, object]) -> dict[str, object]:
    denials = evidence.get("verification_argument_denials")
    recoveries = evidence.get("verification_recoveries")
    if not isinstance(denials, int) or denials < 1:
        return {}
    return {
        "verification_note": (
            "The requested command arguments were denied; a permitted verification "
            f"command later succeeded ({recoveries or 0} recovery)."
        )
    }
