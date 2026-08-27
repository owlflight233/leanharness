"""Minimal OpenAI-compatible chat completions transport built directly on HTTPX."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from leanharness.errors import (
    ModelAuthError,
    ModelError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from leanharness.models.config import ModelConfig
from leanharness.models.contracts import ModelEvent, ModelRequest, ModelResponse, ModelUsage

MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


class OpenAICompatibleClient:
    """Translate provider-independent requests to the chat completions protocol."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout = timeout

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run a bounded non-streaming completion."""

        payload = self._build_payload(request, stream=False)
        try:
            async with self._client() as client, client.stream(
                "POST",
                self._config.chat_completions_url,
                headers=self._headers(),
                json=payload,
            ) as response:
                self._raise_for_status(response.status_code)
                body = await _read_bounded(response)
        except ModelError:
            raise
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError("Model request timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError("Model service is unavailable") from exc

        data = _decode_json(body)
        try:
            choice = _first_choice(data)
            message = choice["message"]
            content = message["content"]
        except (KeyError, TypeError) as exc:
            raise ModelProtocolError("Model response is missing assistant content") from exc
        if not isinstance(content, str):
            raise ModelProtocolError("Model response assistant content must be text")

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ModelProtocolError("Model response has an invalid finish reason")
        return ModelResponse(
            content=content,
            finish_reason=finish_reason,
            usage=_parse_usage(data.get("usage")),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Yield normalized events from a streaming chat completion."""

        payload = self._build_payload(request, stream=True)
        sequence = 0
        finish_reason: str | None = None
        saw_done = False

        try:
            async with self._client() as client, client.stream(
                "POST",
                self._config.chat_completions_url,
                headers=self._headers(),
                json=payload,
            ) as response:
                self._raise_for_status(response.status_code)
                yield ModelEvent(type="turn.started", sequence=sequence)
                sequence += 1

                async for data_line in _iter_sse_data(response):
                    if data_line == "[DONE]":
                        saw_done = True
                        break
                    data = _decode_json(data_line.encode("utf-8"))
                    usage = _parse_usage(data.get("usage"))
                    if usage is not None:
                        yield ModelEvent(
                            type="usage.reported",
                            sequence=sequence,
                            usage=usage,
                        )
                        sequence += 1

                    choices = data.get("choices")
                    if not isinstance(choices, list):
                        raise ModelProtocolError("Model stream event is missing choices")
                    if not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        raise ModelProtocolError("Model stream choice is invalid")
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        raise ModelProtocolError("Model stream delta is invalid")
                    content = delta.get("content")
                    if content is not None:
                        if not isinstance(content, str):
                            raise ModelProtocolError("Model stream content must be text")
                        if content:
                            yield ModelEvent(
                                type="content.delta",
                                sequence=sequence,
                                content=content,
                            )
                            sequence += 1
                    candidate_reason = choice.get("finish_reason")
                    if candidate_reason is not None:
                        if not isinstance(candidate_reason, str):
                            raise ModelProtocolError("Model stream has an invalid finish reason")
                        finish_reason = candidate_reason
        except ModelError:
            raise
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError("Model request timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError("Model service is unavailable") from exc

        if not saw_done:
            raise ModelProtocolError("Model stream ended without a completion marker")
        yield ModelEvent(
            type="turn.completed",
            sequence=sequence,
            finish_reason=finish_reason,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _build_payload(self, request: ModelRequest, *, stream: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "stream": stream,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code in {401, 403}:
            raise ModelAuthError("Model authentication failed")
        if status_code == 429:
            raise ModelRateLimitError("Model service rate limit reached")
        if status_code in {408, 504}:
            raise ModelTimeoutError("Model request timed out")
        if status_code >= 500:
            raise ModelUnavailableError("Model service is unavailable")
        if status_code < 200 or status_code >= 300:
            raise ModelProtocolError(f"Model service returned HTTP {status_code}")


async def _read_bounded(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ModelProtocolError("Model response exceeded the size limit")
    return bytes(body)


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) > MAX_RESPONSE_BYTES and b"\n" not in buffer:
            raise ModelProtocolError("Model stream event exceeded the size limit")
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[: newline + 1]
            if len(raw_line) > MAX_RESPONSE_BYTES:
                raise ModelProtocolError("Model stream event exceeded the size limit")
            data = _parse_sse_line(raw_line)
            if data is not None:
                yield data
    if buffer:
        if len(buffer) > MAX_RESPONSE_BYTES:
            raise ModelProtocolError("Model stream event exceeded the size limit")
        data = _parse_sse_line(bytes(buffer).rstrip(b"\r"))
        if data is not None:
            yield data


def _parse_sse_line(raw_line: bytes) -> str | None:
    if not raw_line or raw_line.startswith(b":"):
        return None
    if not raw_line.startswith(b"data:"):
        return None
    try:
        return raw_line[5:].lstrip(b" ").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelProtocolError("Model stream is not valid UTF-8") from exc


def _decode_json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelProtocolError("Model response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ModelProtocolError("Model response must be a JSON object")
    return data


def _first_choice(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelProtocolError("Model response is missing a choice")
    return choices[0]


def _parse_usage(value: object) -> ModelUsage | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ModelProtocolError("Model response usage is invalid")
    return ModelUsage(
        prompt_tokens=_optional_token_count(value.get("prompt_tokens")),
        completion_tokens=_optional_token_count(value.get("completion_tokens")),
        total_tokens=_optional_token_count(value.get("total_tokens")),
    )


def _optional_token_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelProtocolError("Model response token usage is invalid")
    return value
