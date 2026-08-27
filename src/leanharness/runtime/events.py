"""Public runtime events that never contain hidden reasoning or raw file contents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RuntimeEventType = Literal[
    "run.started",
    "step.started",
    "assistant.progress",
    "tool.requested",
    "tool.started",
    "tool.completed",
    "step.completed",
    "usage.reported",
    "run.completed",
    "run.incomplete",
    "run.failed",
    "run.cancelled",
]


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    type: RuntimeEventType
    sequence: int
    run_id: str
    step: int | None = None
    summary: str | None = None
    tool: str | None = None
    metadata: dict[str, object] | None = None
    answer: str | None = None
    usage: dict[str, int | None] | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "sequence": self.sequence,
            "run_id": self.run_id,
        }
        for key in ("step", "summary", "tool", "metadata", "answer", "usage"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.error_code:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message or "Run failed",
            }
        return payload
