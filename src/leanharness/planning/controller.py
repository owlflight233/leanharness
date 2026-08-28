"""Execute a confirmed plan as ordered tasks inside one coding runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.planning.contracts import Plan, PlanStep, PlanStepState
from leanharness.runtime import CodingAgent, RuntimeEvent
from leanharness.runtime.completion import TaskRequirements
from leanharness.runtime.loop import RuntimeModelClient
from leanharness.tools import ToolRegistry

PlanEventType = str
StepUpdater = Callable[[str, PlanStepState, dict[str, object] | None, str | None], None]


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
    ) -> None:
        if not plan.run_id:
            raise ValueError("Confirmed plan must be attached to a run")
        self.plan = plan
        self._sequence = initial_sequence
        self._on_step = on_step
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
            )
            return
        last_answer: str | None = None
        completed_titles = [
            step.title for step in self.plan.steps if step.state is PlanStepState.COMPLETED
        ]
        for index, step in enumerate(enabled):
            self._update_step(step, PlanStepState.RUNNING)
            yield self._event(
                "plan.step.started",
                step=step.sequence,
                summary=step.title,
                metadata={"step_id": step.id, "instruction": step.instruction},
            )
            task = _step_task(self.plan, step, completed_titles)
            requirements = TaskRequirements.infer(step.instruction)
            self.agent.set_event_sequence(self._sequence)
            if index == 0:
                self.agent.task_requirements = requirements
            stream = (
                self.agent.run(task)
                if index == 0
                else self.agent.continue_task(
                    task,
                    requirements=requirements,
                )
            )
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
                # one contiguous public sequence for the persisted audit stream.
                yield replace(event, sequence=self._next_sequence())
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
                self._update_step(step, PlanStepState.COMPLETED, evidence=evidence)
                last_answer = terminal.answer or last_answer
                completed_titles.append(step.title)
                yield self._event(
                    "plan.step.completed",
                    step=step.sequence,
                    summary=step.title,
                    metadata={"step_id": step.id, "evidence": evidence},
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
                yield replace(terminal, sequence=self._next_sequence())
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
            yield replace(terminal, sequence=self._next_sequence())
            return
        yield self._event("plan.completed", summary="All plan steps completed")
        yield self._runtime_event(
            "run.completed",
            answer=last_answer or "All enabled plan steps are complete.",
            summary="Plan completed",
        )

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


def _terminal_evidence(event: RuntimeEvent) -> dict[str, object]:
    metadata = event.metadata or {}
    evidence = metadata.get("evidence")
    return evidence if isinstance(evidence, dict) else {}
