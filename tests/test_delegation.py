from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from leanharness.models import ModelRequest, ModelResponse, ToolCall
from leanharness.permissions import PermissionMode
from leanharness.runtime import CodingAgent
from leanharness.runtime.delegation import (
    DELEGATE_ANALYSIS_TOOL_NAME,
    ParallelAnalysisTool,
    ScopedReadOnlyToolRegistry,
    SubtaskRequest,
    SubtaskResult,
    SubtaskStatus,
    SubtaskUsage,
)


def _delegate_call(tasks: list[dict[str, object]]) -> ToolCall:
    return ToolCall("delegate-1", DELEGATE_ANALYSIS_TOOL_NAME, {"tasks": tasks})


def _task(name: str, scope: str = ".") -> dict[str, object]:
    return {"task": name, "scope": [scope], "expected_output": "facts"}


def test_delegation_validates_batch_and_workspace_scope(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()

    async def runner(request: SubtaskRequest) -> SubtaskResult:
        raise AssertionError(request)

    tool = ParallelAnalysisTool(tmp_path, runner)
    requests = tool.prepare(_delegate_call([_task("Inspect entry", "src")]))

    assert len(requests) == 1
    assert requests[0].scope == ("src",)
    with pytest.raises(Exception, match="between one and five"):
        tool.prepare(_delegate_call([]))
    with pytest.raises(Exception, match="inside the workspace"):
        tool.prepare(_delegate_call([_task("Escape", "../outside")]))


def test_delegation_runs_five_workers_concurrently_and_returns_stable_order(
    tmp_path: Path,
) -> None:
    started: list[int] = []
    all_started = asyncio.Event()

    async def runner(request: SubtaskRequest) -> SubtaskResult:
        started.append(request.index)
        if len(started) == 5:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        await asyncio.sleep((4 - request.index) * 0.002)
        return SubtaskResult(
            request=request,
            status=SubtaskStatus.COMPLETED,
            summary=f"result-{request.index}",
            facts=(f"fact-{request.index}",),
            usage=SubtaskUsage(input_tokens=request.index + 1, output_tokens=1),
        )

    async def run() -> tuple[object, tuple[SubtaskResult, ...]]:
        tool = ParallelAnalysisTool(tmp_path, runner)
        call = _delegate_call([_task(f"task-{index}") for index in range(5)])
        requests = tool.prepare(call)
        return await asyncio.wait_for(tool.execute_batch(call, requests), timeout=1)

    result, results = asyncio.run(run())

    assert started == [0, 1, 2, 3, 4]
    assert [item.summary for item in results] == [f"result-{index}" for index in range(5)]
    assert result.ok is True
    assert [
        item["summary"] for item in result.data["delegated_analysis_evidence"]
    ] == [f"result-{index}" for index in range(5)]


def test_scoped_worker_registry_is_read_only_and_enforces_assigned_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text("pass", encoding="utf-8")
    registry = ScopedReadOnlyToolRegistry(tmp_path, ("src",))

    assert {definition.name for definition in registry.definitions} == {
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "git_inspect",
    }
    allowed = registry.execute(
        ToolCall("read-1", "workspace_read", {"path": "src/app.py"})
    )
    denied = registry.execute(
        ToolCall("read-2", "workspace_read", {"path": "tests/test_app.py"})
    )

    assert allowed.ok is True
    assert denied.ok is False
    assert denied.error and denied.error.code == "SUBTASK_SCOPE_DENIED"


class DelegationModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        delegated = "delegated repository analyst" in request.messages[0].content
        if delegated:
            task = next(
                message.content for message in request.messages if message.role == "user"
            )
            if not any(message.role == "tool" for message in request.messages):
                path = "src.py" if "entry" in task else "tests.py"
                return ModelResponse(
                    content="Inspecting assigned evidence.",
                    tool_calls=(
                        ToolCall(
                            id=f"read-{path}",
                            name="workspace_read",
                            arguments={"path": path},
                        ),
                    ),
                )
            return ModelResponse(
                content=json.dumps(
                    {
                        "summary": f"completed {task}",
                        "facts": [f"confirmed {task}"],
                        "blockers": [],
                    }
                )
            )
        delegated_result = next(
            (
                message
                for message in request.messages
                if message.role == "tool"
                and "delegated_analysis_evidence" in message.content
            ),
            None,
        )
        if delegated_result is None:
            return ModelResponse(
                content="Delegating independent checks.",
                tool_calls=(
                    _delegate_call(
                        [_task("inspect entry", "src.py"), _task("inspect tests", "tests.py")]
                    ),
                ),
            )
        return ModelResponse(content="Both delegated checks completed.")


class WorkerCompletionContractModel:
    """Ensure a worker keeps its outcome tool available through its final step."""

    def __init__(self) -> None:
        self.worker_requests: list[ModelRequest] = []
        self.parent_delegated = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if "delegated repository analyst" not in request.messages[0].content:
            if self.parent_delegated:
                return ModelResponse(content="Delegated evidence is sufficient.")
            self.parent_delegated = True
            return ModelResponse(
                content="Delegating analysis.",
                tool_calls=(
                    _delegate_call([_task("inspect entry", "src.py")]),
                ),
            )
        self.worker_requests.append(request)
        if not any(message.role == "tool" for message in request.messages):
            return ModelResponse(
                content="Reading the assigned file.",
                tool_calls=(
                    ToolCall("read-entry", "workspace_read", {"path": "src.py"}),
                ),
            )
        return ModelResponse(
            content="",
            tool_calls=(
                ToolCall(
                    "worker-outcome",
                    "report_run_outcome",
                    {
                        "status": "completed",
                        "answer": json.dumps(
                            {
                                "summary": "Entry inspected",
                                "facts": ["The entry file is readable."],
                                "blockers": [],
                            }
                        ),
                    },
                ),
            ),
        )


def test_worker_completion_keeps_outcome_tool_available(tmp_path: Path) -> None:
    (tmp_path / "src.py").write_text("print('ok')", encoding="utf-8")
    model = WorkerCompletionContractModel()
    agent = CodingAgent(tmp_path, model, enable_delegation=True, max_steps=4)

    async def run():
        return [event async for event in agent.run("Inspect entry")]

    events = asyncio.run(run())

    assert events[-1].type == "run.completed"
    assert any(
        definition.name == "report_run_outcome"
        for request in model.worker_requests
        for definition in request.tools
    )
    assert not any(request.tool_choice == "none" for request in model.worker_requests)


def test_incomplete_worker_reports_budget_boundary_instead_of_invalid_json(
    tmp_path: Path,
) -> None:
    (tmp_path / "src.py").write_text("print('ok')", encoding="utf-8")

    class ExhaustedWorkerModel:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if "delegated repository analyst" not in request.messages[0].content:
                self.parent_calls += 1
                if self.parent_calls == 1:
                    return ModelResponse(
                        content="Delegating analysis.",
                        tool_calls=(
                            _delegate_call([_task("inspect entry", "src.py")]),
                        ),
                    )
                return ModelResponse(content="Partial delegated evidence.")
            return ModelResponse(
                content="Reading assigned evidence.",
                tool_calls=(
                    ToolCall("read-entry", "workspace_read", {"path": "src.py"}),
                ),
            )

    model = ExhaustedWorkerModel()
    agent = CodingAgent(tmp_path, model, enable_delegation=True, max_steps=3)

    async def run():
        return [event async for event in agent.run("Inspect entry")]

    events = asyncio.run(run())
    subtask = next(event for event in events if event.type == "subtask.failed")

    assert subtask.metadata and subtask.metadata["status"] in {"incomplete", "failed"}
    assert subtask.metadata["error_code"] != "SUBTASK_RESULT_INVALID"
    assert subtask.summary != "子任务未返回有效的结构化证据"


def test_parent_receives_only_structured_delegated_evidence(tmp_path: Path) -> None:
    (tmp_path / "src.py").write_text("APP_SECRET_SOURCE = True", encoding="utf-8")
    (tmp_path / "tests.py").write_text("TEST_SECRET_SOURCE = True", encoding="utf-8")
    model = DelegationModel()
    agent = CodingAgent(
        tmp_path,
        model,
        permission_mode=PermissionMode.UNRESTRICTED,
        enable_delegation=True,
        max_steps=6,
    )

    async def run():
        return [event async for event in agent.run("Review entry and tests")]

    events = asyncio.run(run())
    parent_requests = [
        request
        for request in model.requests
        if "delegated repository analyst" not in request.messages[0].content
    ]
    final_parent_context = json.dumps(
        [message.content for message in parent_requests[-1].messages], ensure_ascii=False
    )

    assert events[-1].type == "run.completed"
    assert [event.type for event in events].count("subtask.requested") == 2
    assert [event.type for event in events].count("subtask.started") == 2
    assert [event.type for event in events].count("subtask.completed") == 2
    assert "delegated_analysis_evidence" in final_parent_context
    assert "APP_SECRET_SOURCE" not in final_parent_context
    assert "TEST_SECRET_SOURCE" not in final_parent_context
    assert [
        event.metadata["subtask_index"]
        for event in events
        if event.type == "subtask.completed"
    ] == [0, 1]


def test_parent_cancellation_cancels_parallel_workers(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = 0

    async def runner(request: SubtaskRequest) -> SubtaskResult:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    async def run() -> None:
        tool = ParallelAnalysisTool(tmp_path, runner)
        call = _delegate_call([_task("one"), _task("two")])
        requests = tool.prepare(call)
        execution = asyncio.create_task(tool.execute_batch(call, requests))
        await started.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

    asyncio.run(run())
    assert cancelled == 2


def test_parent_run_caps_total_subtasks_at_five(tmp_path: Path) -> None:
    model = DelegationModel()
    agent = CodingAgent(
        tmp_path,
        model,
        permission_mode=PermissionMode.INSPECT,
        enable_delegation=True,
        max_steps=6,
    )

    async def run():
        return [event async for event in agent.run("Review all modules")]

    events = asyncio.run(run())
    assert sum(event.type == "subtask.requested" for event in events) <= 5
