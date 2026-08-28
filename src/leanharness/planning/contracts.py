"""Public plan-domain records and lifecycle states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PlanState(StrEnum):
    DRAFT = "DRAFT"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PlanStepState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class PlanStep:
    id: str
    sequence: int
    title: str
    instruction: str
    enabled: bool = True
    state: PlanStepState = PlanStepState.PENDING
    evidence: dict[str, object] | None = None
    error_code: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    session_id: str
    title: str
    task: str
    state: PlanState
    version: int
    source_markdown: str
    run_id: str | None
    created_at: str
    updated_at: str
    confirmed_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    steps: tuple[PlanStep, ...] = ()


PlanRecord = Plan
PlanStepRecord = PlanStep
PlanVersion = int

