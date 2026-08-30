import asyncio

import pytest

from leanharness.application.model_gateway import check_model
from leanharness.errors import ModelProtocolError
from leanharness.models import ModelRequest, ModelResponse

ENV = {
    "LEANHARNESS_MODEL_BASE_URL": "https://models.example.test/v1",
    "LEANHARNESS_MODEL_NAME": "example-model",
}


class FakeClient:
    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.request: ModelRequest | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.request = request
        return self.response


def test_model_check_uses_small_request_and_safe_result() -> None:
    client = FakeClient(ModelResponse(content="available"))

    result = asyncio.run(check_model(environ=ENV, client_factory=lambda _config: client))

    assert result.status == "ok"
    assert result.model == "example-model"
    assert result.latency_ms >= 0
    assert client.request is not None
    assert client.request.max_tokens == 16
    assert client.request.messages[0].role == "user"


def test_model_check_rejects_empty_reply() -> None:
    client = FakeClient(ModelResponse(content="  "))
    with pytest.raises(ModelProtocolError, match="empty"):
        asyncio.run(check_model(environ=ENV, client_factory=lambda _config: client))


def test_model_check_accepts_reasoning_only_probe_reply() -> None:
    """Thinking models may consume a tiny probe entirely as hidden reasoning."""

    client = FakeClient(ModelResponse(content="", reasoning_content="internal"))

    result = asyncio.run(check_model(environ=ENV, client_factory=lambda _config: client))

    assert result.status == "ok"
