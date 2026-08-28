"""Bounded model context with paired messages and structured evidence capsules."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from leanharness.models import ModelMessage

DEFAULT_CONTEXT_CHARS = 160_000


@dataclass(frozen=True, slots=True)
class ContextCompression:
    compressed_messages: int
    saved_chars: int


class ContextBudgetError(RuntimeError):
    pass


class ContextStore:
    def __init__(self, *, max_chars: int = DEFAULT_CONTEXT_CHARS) -> None:
        if max_chars < 4_096:
            raise ValueError("Context budget is too small")
        self.max_chars = max_chars
        self._messages: list[ModelMessage] = []

    @property
    def messages(self) -> tuple[ModelMessage, ...]:
        return tuple(self._messages)

    def append(self, message: ModelMessage) -> None:
        self._messages.append(message)

    def replace(self, messages: Iterable[ModelMessage]) -> None:
        """Replace a completed context at a safe protocol boundary."""
        self._messages = list(messages)

    def compact(self, *, preserve_recent_messages: int = 10) -> ContextCompression:
        before = self._size()
        compressed = 0
        protected_start = max(0, len(self._messages) - preserve_recent_messages)
        for index in range(protected_start):
            if self._size() <= self.max_chars:
                break
            message = self._messages[index]
            if message.role == "tool" and len(message.content) > 160:
                self._messages[index] = ModelMessage(
                    role="tool",
                    content=_capsule(message),
                    tool_call_id=message.tool_call_id,
                )
                compressed += 1
        after = self._size()
        if after > self.max_chars:
            raise ContextBudgetError("Context budget exceeded after structured compression")
        return ContextCompression(
            compressed_messages=compressed,
            saved_chars=max(0, before - after),
        )

    def _size(self) -> int:
        size = 0
        for message in self._messages:
            size += len(message.content) + 64
            for call in message.tool_calls:
                size += len(call.id) + len(call.name) + len(
                    json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
                )
        return size


def _capsule(message: ModelMessage) -> str:
    details: dict[str, object] = {
        "sha256": hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
        "chars": len(message.content),
        "re_read": True,
    }
    try:
        value = json.loads(message.content)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        for key in ("ok", "tool"):
            if key in value:
                details[key] = value[key]
        error = value.get("error")
        if isinstance(error, dict) and "code" in error:
            details["error_code"] = error["code"]
        result = value.get("result")
        if isinstance(result, dict):
            for key in (
                "path",
                "start_line",
                "line_count",
                "query",
                "matches",
                "files_scanned",
            ):
                if key in result:
                    item = result[key]
                    details[key] = (
                        len(item) if key == "matches" and isinstance(item, list) else item
                    )
    return json.dumps(
        {"evidence_capsule": details}, ensure_ascii=False, separators=(",", ":")
    )
