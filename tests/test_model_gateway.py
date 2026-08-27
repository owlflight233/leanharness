import asyncio
from collections.abc import AsyncIterator

import pytest

from leanharness.application.model_gateway import check_model, stream_chat, validate_chat_message
from leanharness.errors import ChatInputError, ModelAuthError, ModelProtocolError
from leanharness.models import ModelEvent, ModelRequest, ModelResponse

ENV = {
    "LEANHARNESS_MODEL_BASE_URL": "https://models.example.test/v1",
    "LEANHARNESS_MODEL_NAME": "example-model",
}


class FakeClient:
    def __init__(
        self,
        *,
        response: ModelResponse | None = None,
        events: tuple[ModelEvent, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.events = events
        self.error = error
        self.request: ModelRequest | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.request = request
        for event in self.events:
            yield event
        if self.error:
            raise self.error


def test_model_check_uses_small_request_and_safe_result() -> None:
    client = FakeClient(response=ModelResponse(content="available"))

    result = asyncio.run(check_model(environ=ENV, client_factory=lambda _config: client))

    assert result.status == "ok"
    assert result.model == "example-model"
    assert result.latency_ms >= 0
    assert client.request is not None
    assert client.request.max_tokens == 16
    assert client.request.messages[0].role == "user"


def test_model_check_rejects_empty_reply() -> None:
    client = FakeClient(response=ModelResponse(content="  "))
    with pytest.raises(ModelProtocolError, match="empty"):
        asyncio.run(check_model(environ=ENV, client_factory=lambda _config: client))


@pytest.mark.parametrize("message", ["", "   ", "x" * 32_001])
def test_chat_input_is_bounded(message: str) -> None:
    with pytest.raises(ChatInputError):
        validate_chat_message(message)


def test_stream_chat_normalizes_failure_after_partial_output() -> None:
    client = FakeClient(
        events=(
            ModelEvent(type="turn.started", sequence=0),
            ModelEvent(type="content.delta", sequence=1, content="partial"),
        ),
        error=ModelAuthError("Model authentication failed"),
    )

    async def collect() -> list[ModelEvent]:
        return [
            event
            async for event in stream_chat(
                "hello",
                client_factory=lambda _config: client,
            )
        ]

    with pytest.MonkeyPatch.context() as monkeypatch:
        for name, value in ENV.items():
            monkeypatch.setenv(name, value)
        events = asyncio.run(collect())

    assert [event.type for event in events] == ["turn.started", "content.delta", "turn.failed"]
    assert events[-1].sequence == 2
    assert events[-1].error_code == "MODEL_AUTH_FAILED"
