"""Application services for model checks and ephemeral single-turn chat."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from leanharness.application.language import language_instruction
from leanharness.errors import ChatInputError, ModelError, ModelProtocolError
from leanharness.models import (
    ModelConfig,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleClient,
    load_model_config,
)

MAX_CHAT_MESSAGE_CHARS = 32_000
CHECK_PROMPT = "Reply with a brief confirmation that you can receive this request."


class ModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


type ModelClientFactory = Callable[[ModelConfig], ModelClient]


@dataclass(frozen=True, slots=True)
class ModelCheckResult:
    status: str
    model: str
    latency_ms: int

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "model": self.model, "latency_ms": self.latency_ms}


async def check_model(
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: ModelClientFactory = OpenAICompatibleClient,
) -> ModelCheckResult:
    """Verify a complete configuration with one deliberately small model request."""

    config = load_model_config(environ)
    client = client_factory(config)
    started = perf_counter()
    response = await client.complete(
        ModelRequest(
            messages=(ModelMessage(role="user", content=CHECK_PROMPT),),
            max_tokens=16,
        )
    )
    if not response.content.strip():
        raise ModelProtocolError("Model check returned an empty response")
    latency_ms = max(0, round((perf_counter() - started) * 1000))
    return ModelCheckResult(status="ok", model=config.model, latency_ms=latency_ms)


async def stream_chat(
    message: str,
    *,
    config: ModelConfig | None = None,
    client_factory: ModelClientFactory = OpenAICompatibleClient,
    language: str = "same",
) -> AsyncIterator[ModelEvent]:
    """Run one stateless user message and normalize post-start failures as events."""

    validated = validate_chat_message(message)
    resolved_config = config or load_model_config()
    client = client_factory(resolved_config)
    sequence = -1
    try:
        async for event in client.stream(
            ModelRequest(
                messages=(
                    ModelMessage(role="system", content=language_instruction(language)),
                    ModelMessage(role="user", content=validated),
                ),
                stream=True,
            )
        ):
            sequence = event.sequence
            yield event
    except ModelError as exc:
        yield ModelEvent(
            type="turn.failed",
            sequence=sequence + 1,
            error_code=exc.code,
            error_message=exc.message,
        )


def validate_chat_message(message: str) -> str:
    """Enforce the deliberately narrow single-turn input contract."""

    if not message.strip():
        raise ChatInputError("Message must not be blank")
    if len(message) > MAX_CHAT_MESSAGE_CHARS:
        raise ChatInputError(f"Message must not exceed {MAX_CHAT_MESSAGE_CHARS} characters")
    return message
