"""Provider-independent messages, responses, usage, and streaming events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

type MessageRole = Literal["system", "user", "assistant", "tool"]
type EventType = Literal[
    "turn.started",
    "content.delta",
    "usage.reported",
    "turn.completed",
    "turn.failed",
]


@dataclass(frozen=True, slots=True)
class ImageContent:
    """One in-memory image supplied to a multimodal model request."""

    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Provider-independent description of a callable local capability."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A validated model request to invoke one named tool."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: MessageRole
    content: str
    images: tuple[ImageContent, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    max_tokens: int | None = None
    stream: bool = False
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: Literal["auto", "none"] | None = None


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
    tool_calls: tuple[ToolCall, ...] = ()
    # Providers may spend a short probe entirely on hidden reasoning. This is
    # retained only for connectivity checks and is never serialized publicly.
    reasoning_content: str = ""


@dataclass(frozen=True, slots=True)
class ModelEvent:
    type: EventType
    sequence: int
    content: str | None = None
    usage: ModelUsage | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)

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
        if self.tool_calls:
            payload["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        return payload
