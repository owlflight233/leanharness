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
from leanharness.models.contracts import (
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)

MAX_RESPONSE_BYTES = 1_048_576
MAX_TOOL_ARGUMENT_BYTES = 32 * 1024
# Provider responses may contain more calls than the runtime will execute in one
# step. Keep the transport protocol-complete and let the runtime apply that policy.
MAX_MODEL_TOOL_CALLS = 64
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
        choice = _first_choice(data)
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelProtocolError("Model response is missing an assistant message")
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        content = message.get("content")
        if content is None and tool_calls:
            content = ""
        if not isinstance(content, str):
            raise ModelProtocolError("Model response assistant content must be text")

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ModelProtocolError("Model response has an invalid finish reason")
        return ModelResponse(
            content=content,
            finish_reason=finish_reason,
            usage=_parse_usage(data.get("usage")),
            tool_calls=tool_calls,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Yield normalized events from a streaming chat completion."""

        payload = self._build_payload(request, stream=True)
        sequence = 0
        finish_reason: str | None = None
        saw_done = False
        tool_buffers: dict[int, dict[str, str]] = {}

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
                    _accumulate_tool_call_deltas(tool_buffers, delta.get("tool_calls"))
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
        tool_calls = _finalize_tool_call_buffers(tool_buffers)
        yield ModelEvent(
            type="turn.completed",
            sequence=sequence,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
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
            "messages": [_serialize_message(message) for message in request.messages],
            "stream": stream,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
            payload["tool_choice"] = request.tool_choice or "auto"
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


def _serialize_message(message: ModelMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            }
            for call in message.tool_calls
        ]
    if message.role == "tool":
        if not message.tool_call_id:
            raise ModelProtocolError("Tool result message is missing a tool call ID")
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _parse_tool_calls(value: object) -> tuple[ToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_MODEL_TOOL_CALLS:
        raise ModelProtocolError("Model response has an invalid number of tool calls")
    calls: list[ToolCall] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or item.get("type", "function") != "function":
            raise ModelProtocolError("Model response tool call is invalid")
        call_id = item.get("id")
        function = item.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            raise ModelProtocolError("Model response tool call has an invalid ID")
        if not isinstance(function, dict):
            raise ModelProtocolError("Model response tool call is missing a function")
        name = function.get("name")
        raw_arguments = function.get("arguments")
        calls.append(_build_tool_call(call_id, name, raw_arguments))
        seen_ids.add(call_id)
    return tuple(calls)


def _accumulate_tool_call_deltas(
    buffers: dict[int, dict[str, str]], value: object
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise ModelProtocolError("Model stream tool calls are invalid")
    for item in value:
        if not isinstance(item, dict):
            raise ModelProtocolError("Model stream tool call is invalid")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ModelProtocolError("Model stream tool call has an invalid index")
        if index >= MAX_MODEL_TOOL_CALLS:
            raise ModelProtocolError("Model response has too many tool calls")
        buffer = buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = item.get("id")
        if call_id is not None:
            if not isinstance(call_id, str):
                raise ModelProtocolError("Model stream tool call has an invalid ID")
            buffer["id"] += call_id
        call_type = item.get("type")
        if call_type is not None and call_type != "function":
            raise ModelProtocolError("Model stream tool call type is unsupported")
        function = item.get("function")
        if function is not None:
            if not isinstance(function, dict):
                raise ModelProtocolError("Model stream tool function is invalid")
            for field_name in ("name", "arguments"):
                fragment = function.get(field_name)
                if fragment is not None:
                    if not isinstance(fragment, str):
                        raise ModelProtocolError(
                            f"Model stream tool {field_name} fragment is invalid"
                        )
                    buffer[field_name] += fragment
        if len(buffer["arguments"].encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise ModelProtocolError("Model tool arguments exceeded the size limit")


def _finalize_tool_call_buffers(buffers: dict[int, dict[str, str]]) -> tuple[ToolCall, ...]:
    if not buffers:
        return ()
    expected = list(range(len(buffers)))
    if sorted(buffers) != expected:
        raise ModelProtocolError("Model stream tool call indexes are not contiguous")
    calls = tuple(
        _build_tool_call(buffer["id"], buffer["name"], buffer["arguments"])
        for _, buffer in sorted(buffers.items())
    )
    if len({call.id for call in calls}) != len(calls):
        raise ModelProtocolError("Model response tool call IDs must be unique")
    return calls


def _build_tool_call(call_id: object, name: object, raw_arguments: object) -> ToolCall:
    if not isinstance(call_id, str) or not call_id:
        raise ModelProtocolError("Model response tool call is missing an ID")
    if not isinstance(name, str) or not name:
        raise ModelProtocolError("Model response tool call is missing a name")
    if not isinstance(raw_arguments, str):
        raise ModelProtocolError("Model response tool arguments must be JSON text")
    if len(raw_arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
        raise ModelProtocolError("Model tool arguments exceeded the size limit")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ModelProtocolError("Model tool arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ModelProtocolError("Model tool arguments must be a JSON object")
    return ToolCall(id=call_id, name=name, arguments=arguments)
