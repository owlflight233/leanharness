"""Provider-independent messages, responses, usage, and streaming events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type MessageRole = Literal["system", "user", "assistant"]
type EventType = Literal[
    "turn.started",
    "content.delta",
    "usage.reported",
    "turn.completed",
    "turn.failed",
]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    max_tokens: int | None = None
    stream: bool = False


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    finish_reason: str | None = None
    usage: ModelUsage | None = None


@dataclass(frozen=True, slots=True)
class ModelEvent:
    type: EventType
    sequence: int
    content: str | None = None
    usage: ModelUsage | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"type": self.type, "sequence": self.sequence}
        if self.content is not None:
            payload["content"] = self.content
        if self.usage is not None:
            payload["usage"] = self.usage.to_dict()
        if self.finish_reason is not None:
            payload["finish_reason"] = self.finish_reason
        if self.error_code is not None:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message or "Model request failed",
            }
        return payload
