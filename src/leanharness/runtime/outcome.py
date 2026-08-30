"""Model-owned terminal decisions for a coding run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from leanharness.models import ToolCall, ToolDefinition

OUTCOME_TOOL_NAME = "report_run_outcome"
MAX_OUTCOME_ANSWER_CHARS = 32_000

OUTCOME_TOOL = ToolDefinition(
    name=OUTCOME_TOOL_NAME,
    description=(
        "Finish the current task. Choose completed only when the user's requested result "
        "has been achieved; otherwise choose incomplete and explain the concrete blocker. "
        "Call this control action alone, after all required workspace actions are finished."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["completed", "incomplete"],
                "description": "Whether the user's requested task is actually complete.",
            },
            "answer": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_OUTCOME_ANSWER_CHARS,
                "description": "The final user-facing result or incomplete-task report.",
            },
        },
        "required": ["status", "answer"],
    },
)


class OutcomeStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: OutcomeStatus
    answer: str


class OutcomeProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_outcome(call: ToolCall) -> RunOutcome:
    """Validate the model's explicit terminal decision."""

    if call.name != OUTCOME_TOOL_NAME:
        raise OutcomeProtocolError("OUTCOME_TOOL_INVALID", "Unknown outcome control action")
    raw_status = call.arguments.get("status")
    raw_answer = call.arguments.get("answer")
    try:
        status = OutcomeStatus(raw_status)
    except (TypeError, ValueError) as exc:
        raise OutcomeProtocolError(
            "OUTCOME_STATUS_INVALID",
            "Outcome status must be completed or incomplete",
        ) from exc
    if not isinstance(raw_answer, str) or not raw_answer.strip():
        raise OutcomeProtocolError(
            "OUTCOME_ANSWER_INVALID",
            "Outcome answer must be non-empty text",
        )
    answer = raw_answer.strip()
    if len(answer) > MAX_OUTCOME_ANSWER_CHARS:
        raise OutcomeProtocolError(
            "OUTCOME_ANSWER_INVALID",
            f"Outcome answer must not exceed {MAX_OUTCOME_ANSWER_CHARS} characters",
        )
    return RunOutcome(status=status, answer=answer)
