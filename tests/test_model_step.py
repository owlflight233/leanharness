from __future__ import annotations

import asyncio

from leanharness.context import ContextStore
from leanharness.errors import ModelProtocolError
from leanharness.models import ModelMessage, ModelRequest, ModelResponse
from leanharness.runtime.metrics import RunMetrics
from leanharness.runtime.model_step import (
    ModelStepExecutor,
    ProjectionSignal,
    ProtocolRepairSignal,
    RequestStartedSignal,
    ResponseSignal,
)
from leanharness.runtime.recovery import ModelProtocolRecovery


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def request_builder(messages: tuple[ModelMessage, ...], summary_round: bool) -> ModelRequest:
    return ModelRequest(
        messages=messages,
        max_tokens=100,
        tools=(),
        tool_choice="none" if summary_round else "auto",
    )


def make_executor(model: ScriptedModel):
    context = ContextStore(max_chars=20_000)
    context.append(ModelMessage(role="system", content="system constraints"))
    context.append(ModelMessage(role="user", content="inspect the project"))
    metrics = RunMetrics()
    recovery = ModelProtocolRecovery()
    executor = ModelStepExecutor(
        context=context,
        model_client=model,
        metrics=metrics,
        protocol_recovery=recovery,
        request_builder=request_builder,
        language="en",
    )
    return executor, context, metrics


async def collect(executor: ModelStepExecutor):
    return [
        signal
        async for signal in executor.execute(history_sources=(), summary_round=False)
    ]


def test_model_step_emits_projection_before_request_and_response() -> None:
    model = ScriptedModel([ModelResponse(content="done")])
    executor, _, metrics = make_executor(model)

    signals = asyncio.run(collect(executor))

    assert isinstance(signals[0], ProjectionSignal)
    assert isinstance(signals[1], RequestStartedSignal)
    assert isinstance(signals[2], ResponseSignal)
    assert signals[2].response.content == "done"
    assert metrics.model_calls == 1
    assert metrics.projected_messages == 2


def test_model_step_turns_first_protocol_error_into_safe_repair() -> None:
    model = ScriptedModel([ModelProtocolError("malformed secret provider body")])
    executor, context, metrics = make_executor(model)

    signals = asyncio.run(collect(executor))

    assert isinstance(signals[-1], ProtocolRepairSignal)
    assert "malformed secret provider body" not in signals[-1].repair.message.content
    assert context.messages[-1] == signals[-1].repair.message
    assert metrics.model_calls == 1


def test_model_step_raises_second_protocol_error() -> None:
    recovery = ModelProtocolRecovery()
    assert recovery.request("en") is not None
    context = ContextStore(max_chars=20_000)
    context.append(ModelMessage(role="user", content="task"))
    executor = ModelStepExecutor(
        context=context,
        model_client=ScriptedModel([ModelProtocolError("still malformed")]),
        metrics=RunMetrics(),
        protocol_recovery=recovery,
        request_builder=request_builder,
        language="en",
    )

    try:
        asyncio.run(collect(executor))
    except ModelProtocolError as exc:
        assert exc.message == "still malformed"
    else:
        raise AssertionError("second protocol error should be terminal")
