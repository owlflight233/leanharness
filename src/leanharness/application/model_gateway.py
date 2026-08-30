"""Application service for checking model connectivity."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from leanharness.errors import ModelProtocolError
from leanharness.models import (
    ModelConfig,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpenAICompatibleClient,
    load_model_config,
)

CHECK_PROMPT = "Reply with a brief confirmation that you can receive this request."


class ModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


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
    if not response.content.strip() and not response.reasoning_content.strip():
        raise ModelProtocolError("Model check returned an empty response")
    latency_ms = max(0, round((perf_counter() - started) * 1000))
    return ModelCheckResult(status="ok", model=config.model, latency_ms=latency_ms)
