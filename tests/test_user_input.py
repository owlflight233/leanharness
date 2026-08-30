from __future__ import annotations

import asyncio

import pytest

from leanharness.models import ModelRequest, ModelResponse, ToolCall
from leanharness.runtime import CodingAgent, RunState
from leanharness.runtime.outcome import OUTCOME_TOOL_NAME
from leanharness.runtime.state import transition
from leanharness.runtime.user_input import (
    UserInputCoordinator,
    UserInputProtocolError,
    parse_user_input_call,
)


def input_call(**arguments: object) -> ToolCall:
    return ToolCall("input-1", "request_user_input", arguments)


def test_parse_user_input_call_accepts_two_or_three_bounded_options() -> None:
    question, options = parse_user_input_call(
        input_call(
            question="Which target should be changed?",
            options=[
                {"label": "API", "description": "Change the backend API."},
                {"label": "Web", "description": "Change the web client."},
            ],
        )
    )

    assert question == "Which target should be changed?"
    assert [option.label for option in options] == ["API", "Web"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"question": "Q", "options": []},
        {"question": "Q", "options": [{"label": "A", "description": "one"}]},
        {
            "question": "Q",
            "options": [
                {"label": "A", "description": "one", "extra": True},
                {"label": "B", "description": "two"},
            ],
        },
    ],
)
def test_parse_user_input_call_rejects_ambiguous_shapes(arguments: dict[str, object]) -> None:
    with pytest.raises(UserInputProtocolError) as raised:
        parse_user_input_call(input_call(**arguments))
    assert raised.value.code == "INPUT_INVALID_FORMAT"


def test_user_input_coordinator_resolves_once_with_bounded_answer() -> None:
    async def scenario():
        coordinator = UserInputCoordinator(timeout_seconds=1)
        question, options = parse_user_input_call(
            input_call(
                question="Choose target",
                options=[
                    {"label": "A", "description": "First target"},
                    {"label": "B", "description": "Second target"},
                ],
            )
        )
        request = coordinator.request(
            run_id="run-1",
            session_id="session-1",
            tool_call_id="input-1",
            question=question,
            options=options,
        )
        coordinator.resolve("run-1", request.id, "  A  ")
        answer = await coordinator.wait(request)
        with pytest.raises(UserInputProtocolError) as raised:
            coordinator.resolve("run-1", request.id, "B")
        return request, answer, raised.value.code

    request, answer, code = asyncio.run(scenario())

    assert request.question == "Choose target"
    assert answer == "A"
    assert code == "INPUT_ALREADY_RESOLVED"


def test_user_input_state_is_an_explicit_runtime_boundary() -> None:
    assert transition(RunState.INTERPRETING, RunState.WAITING_INPUT) is RunState.WAITING_INPUT
    assert transition(RunState.WAITING_INPUT, RunState.PREPARING) is RunState.PREPARING


def test_model_requested_answer_returns_as_tool_result_in_same_turn(tmp_path) -> None:
    class Model:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                    return ModelResponse(
                        content="",
                        tool_calls=(
                        input_call(
                            question="Which target?",
                            options=[
                                {"label": "API", "description": "Backend API"},
                                {"label": "Web", "description": "Web client"},
                            ],
                        ),
                    )
                )
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        "outcome-1",
                        OUTCOME_TOOL_NAME,
                        {"status": "completed", "answer": "Selected the API target."},
                    ),
                )
            )

    async def scenario():
        model = Model()
        coordinator = UserInputCoordinator(timeout_seconds=1)
        agent = CodingAgent(tmp_path, model, user_inputs=coordinator)
        events = []
        async for event in agent.run("Choose the correct target"):
            events.append(event)
            if event.type == "input.required":
                coordinator.resolve(agent.run_id, str(event.metadata["input_id"]), "API")
        return model, events

    model, events = asyncio.run(scenario())

    assert events[-1].type == "run.completed"
    assert [event.type for event in events if event.type.startswith("input.")] == [
        "input.required",
        "input.resolved",
    ]
    assert any(tool.name == "request_user_input" for tool in model.requests[0].tools)
    tool_message = next(
        message for message in model.requests[1].messages if message.role == "tool"
    )
    assert '"answer":"API"' in tool_message.content
