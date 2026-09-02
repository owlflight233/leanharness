from __future__ import annotations

import asyncio
from pathlib import Path

from leanharness.context import ContextSource
from leanharness.models import ModelMessage, ModelRequest, ModelResponse, ToolCall
from leanharness.permissions import PermissionMode
from leanharness.planning import Plan, PlanController, PlanState, PlanStep, PlanStepState


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses[len(self.requests) - 1]


def make_plan() -> Plan:
    steps = (
        PlanStep("step-1", 1, "Inspect files", "Inspect the repository"),
        PlanStep("step-2", 2, "Inspect tests", "Inspect the tests"),
    )
    return Plan(
        id="plan-1",
        session_id="session-1",
        title="Demo",
        task="Understand the project",
        state=PlanState.RUNNING,
        version=1,
        source_markdown="# Demo",
        run_id="run-1",
        created_at="now",
        updated_at="now",
        steps=steps,
    )


def tool_response(call_id: str, path: str = ".") -> ModelResponse:
    return ModelResponse(
        content="Inspecting.",
        tool_calls=(
            ToolCall(
                id=call_id,
                name="workspace_list",
                arguments={"path": path},
            ),
        ),
    )


def test_controller_executes_all_steps_in_one_agent_context(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1"),
            ModelResponse(content="Repository inspected."),
            tool_response("call-2"),
            ModelResponse(content="Tests inspected."),
        ]
    )
    updates: list[tuple[str, PlanStepState]] = []
    controller = PlanController(
        make_plan(),
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        language="en",
        max_steps=4,
        on_step=lambda step_id, state, _evidence, _error: updates.append((step_id, state)),
    )

    async def collect():
        return [event async for event in controller.run()]

    events = asyncio.run(collect())
    assert [event.type for event in events if event.type.startswith("plan.step")] == [
        "plan.step.started",
        "plan.step.completed",
        "plan.step.started",
        "plan.step.completed",
    ]
    assert events[-2].type == "plan.completed"
    assert events[-1].type == "run.completed"
    assert [event.sequence for event in events] == list(range(len(events)))
    assert updates[-1] == ("step-2", PlanStepState.COMPLETED)
    assert any(
        "Completed steps: ['Inspect files']" in message.content
        for message in model.requests[2].messages
    )
    assert not any(message.role == "tool" for message in model.requests[2].messages)
    assert events[-1].answer is not None
    assert "## Inspect files" in events[-1].answer
    assert "## Inspect tests" in events[-1].answer
    assert events[-1].metadata["completed_step_ids"] == ["step-1", "step-2"]
    assert events[-1].metadata["evidence"]["observations"] == 2
    assert events[-1].metadata["metrics"]["model_calls"] == 4
    first_tool = next(event for event in events if event.type == "tool.requested")
    assert first_tool.metadata["plan_step"] == 1


def test_controller_projects_public_session_history_into_first_step(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1"),
            ModelResponse(content="Repository inspected."),
            tool_response("call-2"),
            ModelResponse(content="Tests inspected."),
        ]
    )
    controller = PlanController(
        make_plan(),
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        language="en",
        max_steps=4,
        history_sources=(
            ContextSource("message:prior", ModelMessage(role="user", content="Prior task")),
        ),
    )

    async def collect():
        return [event async for event in controller.run()]

    asyncio.run(collect())

    assert any(
        message.role == "user" and message.content == "Prior task"
        for message in model.requests[0].messages
    )


def test_controller_pauses_when_step_is_incomplete(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1"),
            ModelResponse(content="Summary without finishing."),
        ]
    )
    controller = PlanController(
        make_plan(),
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        language="en",
        max_steps=2,
    )

    async def collect():
        return [event async for event in controller.run()]

    events = asyncio.run(collect())
    assert events[-2].type == "plan.paused"
    assert events[-1].type == "run.incomplete"
    assert not any(event.type == "plan.completed" for event in events)


def test_controller_lets_model_choose_with_available_session_tools(
    tmp_path: Path,
) -> None:
    model = ScriptedModel([])
    plan = Plan(
        id="plan-write",
        session_id="session-1",
        title="Write",
        task="Create a project",
        state=PlanState.RUNNING,
        version=1,
        source_markdown="# Write\n1. Create files",
        run_id="run-write",
        created_at="now",
        updated_at="now",
        steps=(PlanStep("step-write", 1, "Create files", "Create app.py"),),
    )
    controller = PlanController(
        plan,
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        language="en",
    )

    async def collect_events():
        return [event async for event in controller.run()]

    events = asyncio.run(collect_events())

    assert model.requests
    assert [definition.name for definition in model.requests[0].tools] == [
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "git_inspect",
        "report_run_outcome",
    ]
    assert events[-1].type == "run.failed"
    assert events[-1].error_code == "RUN_MODEL_FAILED"


def test_controller_budget_is_shared_across_plan_steps(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call-1"),
            ModelResponse(content="First step inspected."),
            tool_response("call-2"),
        ]
    )
    controller = PlanController(
        make_plan(),
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        language="en",
        max_steps=3,
    )

    async def collect():
        return [event async for event in controller.run()]

    events = asyncio.run(collect())
    assert events[-1].type == "run.incomplete"
    assert events[-1].metadata["incomplete_reason"] == "STEP_BUDGET_EXHAUSTED"
    assert len(model.requests) == 3
    assert any(event.type == "plan.step.started" and event.step == 2 for event in events)


def test_default_plan_uses_one_unbounded_agent_loop(tmp_path: Path) -> None:
    responses = [
        ModelResponse(
            content="",
            tool_calls=(
                ToolCall(
                    f"search-{index}",
                    "workspace_search",
                    {"path": ".", "query": f"term-{index}"},
                ),
            ),
        )
        for index in range(25)
    ]
    responses.append(ModelResponse(content="The plan is complete."))
    model = ScriptedModel(responses)
    controller = PlanController(
        make_plan(),
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        language="en",
    )

    async def collect():
        return [event async for event in controller.run()]

    events = asyncio.run(collect())
    assert events[-1].type == "run.completed"
    assert len(model.requests) == 26
    assert "## Inspect files" in events[-1].answer
    assert "## Inspect tests" in events[-1].answer


def test_unbounded_plan_persists_evidence_report_when_model_stops_incomplete(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            tool_response("observe"),
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        "outcome",
                        "report_run_outcome",
                        {"status": "incomplete", "answer": "Temporary model report."},
                    ),
                ),
            ),
        ]
    )
    controller = PlanController(
        make_plan(),
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        language="en",
    )

    async def collect():
        return [event async for event in controller.run()]

    events = asyncio.run(collect())
    assert events[-2].type == "plan.paused"
    assert events[-1].type == "run.incomplete"
    assert events[-1].answer == events[-2].answer
    assert "budget" not in (events[-2].summary or "").lower()
    assert "not complete" in (events[-2].summary or "").lower()
    assert "Temporary model report." not in events[-1].answer
    assert "## Remaining" in events[-1].answer


def test_plan_pauses_with_permission_reason_instead_of_plan_failure(
    tmp_path: Path,
) -> None:
    class PermissionBlockedModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            if self.calls > 1:
                return ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "incomplete",
                            "report_run_outcome",
                            {"status": "incomplete", "answer": "Need write access."},
                        ),
                    ),
                )
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        "missing-write",
                        "workspace_patch",
                        {"patch": "*** Begin Patch\n*** End Patch"},
                    ),
                ),
            )

    controller = PlanController(
        make_plan(),
        tmp_path,
        PermissionBlockedModel(),
        permission_mode=PermissionMode.INSPECT,
        language="en",
        max_steps=None,
    )

    async def collect():
        return [event async for event in controller.run()]

    events = asyncio.run(collect())
    # The unavailable mutating capability is a recoverable pause.  It must not
    # be presented as a broken plan, and the user can resume after changing
    # the session permission.
    assert any(event.type == "plan.paused" for event in events)
    assert not any(event.type == "plan.failed" for event in events)
    terminal = events[-1]
    assert terminal.type == "run.incomplete"
    assert terminal.error_code == "TOOL_NOT_FOUND"
    assert terminal.metadata["incomplete_reason"] == "PERMISSION_REQUIRED"
