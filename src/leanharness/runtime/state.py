"""Explicit runtime states and guarded transitions."""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    REQUESTING_MODEL = "REQUESTING_MODEL"
    INTERPRETING = "INTERPRETING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING_TOOL = "EXECUTING_TOOL"
    COMPLETED = "COMPLETED"
    EXHAUSTED = "EXHAUSTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset(
    {RunState.COMPLETED, RunState.EXHAUSTED, RunState.FAILED, RunState.CANCELLED}
)

ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.PREPARING, RunState.CANCELLED}),
    RunState.PREPARING: frozenset(
        {RunState.REQUESTING_MODEL, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.REQUESTING_MODEL: frozenset(
        {RunState.INTERPRETING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.INTERPRETING: frozenset(
        {
            RunState.PREPARING,
            RunState.WAITING_APPROVAL,
            RunState.EXECUTING_TOOL,
            RunState.COMPLETED,
            RunState.EXHAUSTED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.EXECUTING_TOOL: frozenset(
        {
            RunState.EXECUTING_TOOL,
            RunState.WAITING_APPROVAL,
            RunState.PREPARING,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.WAITING_APPROVAL: frozenset(
        {RunState.EXECUTING_TOOL, RunState.PREPARING, RunState.FAILED, RunState.CANCELLED}
    ),
}


class InvalidTransition(RuntimeError):
    pass


def transition(current: RunState, target: RunState) -> RunState:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransition(f"Invalid runtime transition: {current} -> {target}")
    return target
