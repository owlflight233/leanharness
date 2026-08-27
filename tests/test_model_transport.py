import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from leanharness.errors import (
    ModelAuthError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from leanharness.models import (
    ModelConfig,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleClient,
    ToolCall,
    ToolDefinition,
)

SECRET = "transport-test-secret"


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def config() -> ModelConfig:
    return ModelConfig(
        base_url="https://models.example.test/v1",
        model="example-model",
        api_key=SECRET,
    )


def run(coroutine):
    return asyncio.run(coroutine)


def test_complete_sends_safe_openai_compatible_request() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ready"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))
    response = run(
        client.complete(
            ModelRequest(messages=(ModelMessage(role="user", content="check"),), max_tokens=16)
        )
    )

    assert response.content == "ready"
    assert response.finish_reason == "stop"
    assert response.usage is not None and response.usage.total_tokens == 3
    assert captured == {
        "url": "https://models.example.test/v1/chat/completions",
        "authorization": f"Bearer {SECRET}",
        "body": {
            "model": "example-model",
            "messages": [{"role": "user", "content": "check"}],
            "stream": False,
            "max_tokens": 16,
        },
    }


def test_stream_parses_cross_chunk_unicode_finish_reason_and_usage() -> None:
    content_event = (
        'data: {"choices":[{"delta":{"content":"hello 世界"},"finish_reason":null}]}\n\n'
    ).encode()
    split_at = content_event.index("世".encode()) + 1
    stream = ChunkedStream(
        [
            content_event[:split_at],
            content_event[split_at:],
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3,',
            b'"total_tokens":5}}\n\ndata: [DONE]\n\n',
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, stream=stream)

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))

    async def collect():
        request = ModelRequest(messages=(ModelMessage(role="user", content="hello"),), stream=True)
        return [event async for event in client.stream(request)]

    events = run(collect())

    assert [event.type for event in events] == [
        "turn.started",
        "content.delta",
        "usage.reported",
        "turn.completed",
    ]
    assert events[1].content == "hello 世界"
    assert events[2].usage is not None and events[2].usage.total_tokens == 5
    assert events[3].finish_reason == "stop"
    assert stream.closed is True


@pytest.mark.parametrize(
    ("status", "error_type", "code"),
    [
        (401, ModelAuthError, "MODEL_AUTH_FAILED"),
        (403, ModelAuthError, "MODEL_AUTH_FAILED"),
        (429, ModelRateLimitError, "MODEL_RATE_LIMITED"),
        (500, ModelUnavailableError, "MODEL_UNAVAILABLE"),
        (504, ModelTimeoutError, "MODEL_TIMEOUT"),
    ],
)
def test_http_failures_map_to_stable_safe_errors(status, error_type, code) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"upstream leaked {SECRET}")

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))

    with pytest.raises(error_type) as caught:
        run(client.complete(ModelRequest(messages=(ModelMessage(role="user", content="x"),))))
    assert caught.value.code == code
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    ("raised", "error_type", "code"),
    [
        (httpx.ReadTimeout("slow"), ModelTimeoutError, "MODEL_TIMEOUT"),
        (httpx.ConnectError("offline"), ModelUnavailableError, "MODEL_UNAVAILABLE"),
    ],
)
def test_transport_failures_map_to_stable_errors(raised, error_type, code) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(error_type) as caught:
        run(client.complete(ModelRequest(messages=(ModelMessage(role="user", content="x"),))))
    assert caught.value.code == code


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": 42}}]}),
    ],
)
def test_complete_rejects_malformed_protocol(response: httpx.Response) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelProtocolError):
        run(client.complete(ModelRequest(messages=(ModelMessage(role="user", content="x"),))))


@pytest.mark.parametrize(
    "payload",
    [
        b"data: not-json\n\ndata: [DONE]\n",
        b'data: {"usage":"invalid","choices":[]}\n\ndata: [DONE]\n',
        b'data: {"choices":[{"delta":{"content":42}}]}\n\ndata: [DONE]\n',
        b'data: {"choices":[{"delta":{"content":"partial"}}]}\n',
    ],
)
def test_stream_rejects_malformed_or_incomplete_protocol(payload: bytes) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))

    async def collect():
        request = ModelRequest(messages=(ModelMessage(role="user", content="x"),), stream=True)
        return [event async for event in client.stream(request)]

    with pytest.raises(ModelProtocolError):
        run(collect())


def test_stream_closes_upstream_when_consumer_cancels() -> None:
    stream = ChunkedStream(
        [
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"second"}}]}\n',
            b"data: [DONE]\n",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))

    async def consume_one_delta() -> None:
        request = ModelRequest(messages=(ModelMessage(role="user", content="x"),), stream=True)
        iterator = client.stream(request)
        await anext(iterator)
        await anext(iterator)
        await iterator.aclose()

    run(consume_one_delta())
    assert stream.closed is True


def test_stream_rejects_a_single_oversized_event() -> None:
    payload = b"data: " + (b"x" * 1_048_577) + b"\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))

    async def collect():
        request = ModelRequest(messages=(ModelMessage(role="user", content="x"),), stream=True)
        return [event async for event in client.stream(request)]

    with pytest.raises(ModelProtocolError, match="size limit"):
        run(collect())


def test_complete_serializes_tools_and_linked_tool_results() -> None:
    captured: dict[str, object] = {}
    call = ToolCall(id="call-1", name="workspace_list", arguments={"path": "."})
    definition = ToolDefinition(
        name="workspace_list",
        description="List workspace entries.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                ]
            },
        )

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))
    response = run(
        client.complete(
            ModelRequest(
                messages=(
                    ModelMessage(
                        role="assistant",
                        content="I will inspect files.",
                        tool_calls=(call,),
                    ),
                    ModelMessage(role="tool", content='{"ok":true}', tool_call_id="call-1"),
                ),
                tools=(definition,),
            )
        )
    )

    assert response.content == "done"
    assert captured["tool_choice"] == "auto"
    assert captured["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "workspace_list",
                "description": "List workspace entries.",
                "parameters": definition.parameters,
            },
        }
    ]
    assert captured["messages"] == [
        {
            "role": "assistant",
            "content": "I will inspect files.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "workspace_list", "arguments": '{"path":"."}'},
                }
            ],
        },
        {"role": "tool", "content": '{"ok":true}', "tool_call_id": "call-1"},
    ]


def test_complete_accepts_more_calls_than_the_runtime_step_limit() -> None:
    tool_calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": "workspace_list", "arguments": "{}"},
        }
        for index in range(5)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "", "tool_calls": tool_calls},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))
    response = run(
        client.complete(ModelRequest(messages=(ModelMessage(role="user", content="inspect"),)))
    )

    assert len(response.tool_calls) == 5


def test_stream_assembles_fragmented_tool_calls() -> None:
    stream = ChunkedStream(
        [
            b'data: {"choices":[{"delta":{"content":"I will inspect. ","tool_calls":[',
            b'{"index":0,"id":"call-","type":"function","function":{"name":"workspace_",',
            b'"arguments":"{\\"path\\":\\""}}]},"finish_reason":null}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"1",',
            b'"function":{"name":"read","arguments":"README.md\\"}"}}]},',
            b'"finish_reason":"tool_calls"}]}\n',
            b"data: [DONE]\n",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))

    async def collect():
        request = ModelRequest(
            messages=(ModelMessage(role="user", content="inspect"),),
            tools=(
                ToolDefinition(
                    name="workspace_read",
                    description="Read a file.",
                    parameters={"type": "object"},
                ),
            ),
            stream=True,
        )
        return [event async for event in client.stream(request)]

    events = run(collect())

    assert [event.type for event in events] == [
        "turn.started",
        "content.delta",
        "turn.completed",
    ]
    assert events[-1].finish_reason == "tool_calls"
    assert events[-1].tool_calls == (
        ToolCall(id="call-1", name="workspace_read", arguments={"path": "README.md"}),
    )


@pytest.mark.parametrize(
    "tool_calls",
    [
        [
            {
                "index": 0,
                "type": "function",
                "function": {"name": "workspace_list", "arguments": "{}"},
            }
        ],
        [{"index": 0, "id": "call-1", "type": "function", "function": {"arguments": "{}"}}],
        [
            {
                "index": 0,
                "id": "call-1",
                "type": "function",
                "function": {"name": "workspace_list", "arguments": "[]"},
            }
        ],
    ],
)
def test_stream_rejects_incomplete_or_non_object_tool_calls(tool_calls: object) -> None:
    payload = (
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"tool_calls": tool_calls}, "finish_reason": "tool_calls"}]}
        )
        + "\ndata: [DONE]\n"
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = OpenAICompatibleClient(config(), transport=httpx.MockTransport(handler))

    async def collect():
        return [
            event
            async for event in client.stream(
                ModelRequest(messages=(ModelMessage(role="user", content="x"),), stream=True)
            )
        ]

    with pytest.raises(ModelProtocolError):
        run(collect())
