"""One public-data redaction policy shared by SQLite and JSONL traces."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from leanharness.logging import redact_text

_HIDDEN_REASONING_BLOCK = re.compile(
    r"(?is)<(?:think|thinking)>.*?</(?:think|thinking)>"
)
_FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "headers",
    "environment",
    "env",
    "cookie",
    "preview",
    "patch",
    "diff",
    "stdout",
    "stderr",
    "output",
    "target_hashes",
    "analysis",
    "chain_of_thought",
    "reasoning",
    "thinking",
}


@dataclass(frozen=True, slots=True)
class TraceRedactor:
    secrets: tuple[str, ...] = ()
    max_text_chars: int = 64_000

    @classmethod
    def from_environment(cls) -> TraceRedactor:
        api_key = os.environ.get("LEANHARNESS_MODEL_API_KEY", "")
        return cls(secrets=(api_key,) if api_key else ())

    def text(self, value: str) -> str:
        safe = _HIDDEN_REASONING_BLOCK.sub("[hidden reasoning redacted]", value)
        safe = redact_text(safe)
        for secret in self.secrets:
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
        if len(safe) > self.max_text_chars:
            return safe[: self.max_text_chars] + "...[truncated]"
        return safe

    def payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _redact_mapping(
            payload,
            event_type=str(payload.get("type", "")),
            redactor=self,
        )


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return TraceRedactor.from_environment().payload(payload)


def _redact_mapping(
    value: dict[str, Any], *, event_type: str, redactor: TraceRedactor
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key.casefold() in _FORBIDDEN_KEYS:
            continue
        if key == "content" and event_type.startswith("tool"):
            result[key] = "[tool result redacted]"
        elif isinstance(item, dict):
            result[key] = _redact_mapping(item, event_type=event_type, redactor=redactor)
        elif isinstance(item, list):
            result[key] = [
                _redact_mapping(entry, event_type=event_type, redactor=redactor)
                if isinstance(entry, dict)
                else redactor.text(entry)
                if isinstance(entry, str)
                else entry
                for entry in item[:100]
            ]
        elif isinstance(item, str):
            result[key] = redactor.text(item)
        else:
            result[key] = item
    return result

