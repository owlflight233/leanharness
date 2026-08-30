"""Backward-compatible live context store."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from leanharness.context.projection import (
    DEFAULT_CONTEXT_CHARS,
    ContextBudgetError,
    ContextCompression,
    ContextJournal,
    ContextProjection,
    ContextProjector,
    ContextSource,
    _capsule,
    _message_size,
)
from leanharness.models import ModelMessage


class ContextStore(ContextJournal):
    """Live journal with the original deterministic ``compact`` API."""

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_CONTEXT_CHARS,
        soft_chars: int | None = None,
        semantic_compaction: bool = True,
        summary_sanitizer: Callable[[str], str] | None = None,
    ) -> None:
        if max_chars < 4_096:
            raise ValueError("Context budget is too small")
        super().__init__()
        self.max_chars = max_chars
        self.soft_chars = soft_chars if soft_chars is not None else min(128_000, max_chars)
        self.projector = ContextProjector(
            max_chars=max_chars,
            soft_chars=self.soft_chars,
            semantic_compaction=semantic_compaction,
            summary_sanitizer=summary_sanitizer,
        )

    def compact(self, *, preserve_recent_messages: int = 10) -> ContextCompression:
        """Replace old tool payloads with structured evidence capsules."""
        before = _message_size(self._messages)
        compressed = 0
        protected_start = max(0, len(self._messages) - preserve_recent_messages)
        for index in range(protected_start):
            if _message_size(self._messages) <= self.max_chars:
                break
            message = self._messages[index]
            if message.role == "tool" and len(message.content) > 160:
                self._messages[index] = ModelMessage(
                    role="tool",
                    content=_capsule(message),
                    tool_call_id=message.tool_call_id,
                )
                compressed += 1
        after = _message_size(self._messages)
        if after > self.max_chars:
            raise ContextBudgetError("Context budget exceeded after structured compression")
        if compressed:
            self._generation += 1
        return ContextCompression(
            compressed_messages=compressed,
            saved_chars=max(0, before - after),
        )

    def projection(
        self, history: Iterable[ContextSource | ModelMessage] = ()
    ) -> ContextProjection:
        return self.projector.project(tuple(history), self)
