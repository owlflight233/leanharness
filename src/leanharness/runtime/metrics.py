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
    projected_chars: int = 0
    projected_messages: int = 0
    compressed_steps: int = 0
    compressed_tool_results: int = 0
    semantic_compaction_calls: int = 0
    semantic_compaction_fallbacks: int = 0
    context_generation: int = 0

    def record_usage(self, usage: ModelUsage) -> None:
        self.prompt_tokens += usage.prompt_tokens or 0
        self.completion_tokens += usage.completion_tokens or 0
        self.total_tokens += usage.total_tokens or 0

    def record_projection(
        self,
        *,
        chars: int,
        messages: int,
        compressed_steps: int,
        compressed_tool_results: int,
        semantic_calls: int,
        semantic_fallback: bool,
        generation: int,
    ) -> None:
        self.projected_chars = chars
        self.projected_messages = messages
        self.compressed_steps = max(self.compressed_steps, compressed_steps)
        self.compressed_tool_results = max(
            self.compressed_tool_results, compressed_tool_results
        )
        self.semantic_compaction_calls = semantic_calls
        self.semantic_compaction_fallbacks += int(semantic_fallback)
        self.context_generation = generation

    def to_dict(self) -> dict[str, int]:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    def context_dict(self) -> dict[str, int]:
        return {
            "projected_chars": self.projected_chars,
            "projected_messages": self.projected_messages,
            "compressed_steps": self.compressed_steps,
            "compressed_tool_results": self.compressed_tool_results,
            "semantic_compaction_calls": self.semantic_compaction_calls,
            "semantic_compaction_fallbacks": self.semantic_compaction_fallbacks,
            "context_generation": self.context_generation,
        }
