"""Small, auditable context carried between runs in one local session."""

from __future__ import annotations

import json
from dataclasses import dataclass

MAX_CONTINUATION_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ContinuationContext:
    previous_task: str
    previous_state: str
    changed_files: tuple[str, ...] = ()
    incomplete_reason: str | None = None
    permission_mode: str = "inspect"
    previous_run_permission_mode: str | None = None
    source_run_id: str | None = None
    mutation_required: bool = False
    verification_required: bool = False

    def to_model_message(self) -> str:
        payload = {
            "previous_task": self.previous_task,
            "previous_terminal_state": self.previous_state,
            "changed_files": list(self.changed_files),
            "incomplete_or_error_reason": self.incomplete_reason,
            "current_permission_mode": self.permission_mode,
            "previous_run_permission_mode": self.previous_run_permission_mode,
            "source_run_id": self.source_run_id,
            "requirements": {
                "mutation_required": self.mutation_required,
                "verification_required": self.verification_required,
            },
            "continuation_rule": (
                "The previous answer may reflect an older permission. The current run's "
                "permission snapshot and supplied tools are authoritative."
            ),
        }
        prefix = (
            "Bounded continuation context from the selected prior substantive run. "
            "Use it only to resolve references in the current task; verify workspace state "
            "with tools before making claims:\n"
        )
        rendered = prefix + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered.encode("utf-8")) <= MAX_CONTINUATION_BYTES:
            return rendered
        payload["previous_task"] = _bounded_utf8(self.previous_task, 2048)
        payload["changed_files"] = list(self.changed_files[:20])
        rendered = prefix + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return _bounded_utf8(rendered, MAX_CONTINUATION_BYTES)


def _bounded_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[: max(0, limit - 3)].decode("utf-8", errors="ignore") + "..."
