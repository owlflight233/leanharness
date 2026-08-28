"""Immutable records returned by the local persistence adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from leanharness.planning.contracts import Plan, PlanStep


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    project_id: str
    title: str
    permission_mode: str
    language: str | None
    created_at: str
    updated_at: str
    last_run_state: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    root_path: str
    permission_mode: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: str
    session_id: str
    sequence: int
    role: str
    content: str
    status: str
    created_at: str
    run_id: str | None = None
    kind: str = "chat"
    plan_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    session_id: str
    mode: str
    task: str
    state: str
    max_steps: int
    permission_mode: str
    answer: str | None
    error_code: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    request: dict[str, Any]
    state: str
    requested_at: str
    decided_at: str | None


PlanRecord = Plan
PlanStepRecord = PlanStep
