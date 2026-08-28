"""Provider-neutral run efficiency counters."""

from __future__ import annotations

from dataclasses import dataclass

from leanharness.models import ModelUsage


@dataclass(slots=True)
class RunMetrics:
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def record_usage(self, usage: ModelUsage) -> None:
        self.prompt_tokens += usage.prompt_tokens or 0
        self.completion_tokens += usage.completion_tokens or 0
        self.total_tokens += usage.total_tokens or 0

    def to_dict(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
