from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from leanharness.application.session_gateway import context_history_for_session
from leanharness.context import (
    ContextBudgetError,
    ContextJournal,
    ContextProjector,
    ContextProtocolError,
    ContextSource,
)
from leanharness.errors import ModelContextLengthError
from leanharness.models import ModelMessage, ModelRequest, ModelResponse, ToolCall
from leanharness.runtime import CodingAgent
from leanharness.storage import LocalStore

SUMMARY = json.dumps(
    {
        "objective": "Inspect and update the project",
        "constraints": ["Stay inside the workspace"],
        "decisions": ["Read before editing"],
        "observations": [
            {
                "tool": "workspace_read",
                "path": "README.md",
                "status": "success",
                "hash": "abc123",
            }
        ],
        "changed_files": [],
        "verification": [],
        "blockers": [],
        "pending_actions": ["Run verification"],
    }
)


class SummaryModel:
    def __init__(self, response: str = SUMMARY) -> None:
        self.response = response
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(content=self.response)


def run(coro):
    return asyncio.run(coro)


def history_sources(*, content_size: int = 1_100) -> tuple[ContextSource, ...]:
    result: list[ContextSource] = []
    for index in range(4):
        result.extend(
            (
                ContextSource(
                    f"message:user-{index}",
                    ModelMessage(role="user", content=f"task-{index} " + "u" * content_size),
                ),
                ContextSource(
                    f"message:assistant-{index}",
                    ModelMessage(
                        role="assistant", content=f"answer-{index} " + "a" * content_size
                    ),
                ),
            )
        )
    return tuple(result)


def test_projection_keeps_system_first_and_stable_sources() -> None:
    journal = ContextJournal(
        (
            ModelMessage(role="system", content="rules"),
            ModelMessage(role="user", content="current task"),
        )
    )
    history = (
        ContextSource("message:1", ModelMessage(role="user", content="old task")),
        ContextSource("message:2", ModelMessage(role="assistant", content="old answer")),
    )

    projection = ContextProjector(max_chars=8_000, soft_chars=6_000).project(
        history, journal
    )

    assert [message.content for message in projection.messages] == [
        "rules",
        "old task",
        "old answer",
        "current task",
    ]
    assert projection.source_ids == ("live:0", "message:1", "message:2", "live:1")
    assert projection.digest


def test_deterministic_compaction_preserves_tool_protocol_and_recent_steps() -> None:
    journal = ContextJournal(
        (ModelMessage(role="system", content="rules"), ModelMessage(role="user", content="task"))
    )
    for index in range(8):
        call = ToolCall(f"call-{index}", "workspace_read", {"path": f"file-{index}.py"})
        journal.append(ModelMessage(role="assistant", content="read", tool_calls=(call,)))
        journal.append(
            ModelMessage(
                role="tool",
                tool_call_id=call.id,
                content=json.dumps(
                    {
                        "ok": True,
                        "tool": "workspace_read",
                        "result": {
                            "path": f"file-{index}.py",
                            "line_count": 200,
                            "content": "x" * 1_000,
                        },
                    }
                ),
            )
        )

    projection = ContextProjector(max_chars=8_000, soft_chars=5_000).project((), journal)
    assistants = [message for message in projection.messages if message.tool_calls]
    tools = [message for message in projection.messages if message.role == "tool"]

    assert len(assistants) == len(tools) == 8
    assert [call.id for message in assistants for call in message.tool_calls] == [
        message.tool_call_id for message in tools
    ]
    assert any("evidence_capsule" in message.content for message in tools[:-2])
    assert all("evidence_capsule" not in message.content for message in tools[-2:])


@pytest.mark.parametrize(
    "messages",
    [
        (
            ModelMessage(
                role="tool", tool_call_id="missing", content='{"ok":false}'
            ),
        ),
        (
            ModelMessage(
                role="assistant",
                content="",
                tool_calls=(ToolCall("call-1", "workspace_list", {"path": "."}),),
            ),
        ),
        (
            ModelMessage(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall("call-1", "workspace_list", {"path": "."}),
                    ToolCall("call-1", "workspace_read", {"path": "README.md"}),
                ),
            ),
        ),
    ],
)
def test_projection_rejects_unclosed_or_orphaned_tool_messages(
    messages: tuple[ModelMessage, ...],
) -> None:
    with pytest.raises(ContextProtocolError):
        ContextProjector(max_chars=8_000, soft_chars=6_000).project(
            (), ContextJournal(messages)
        )


def test_semantic_compaction_is_bounded_cached_and_tool_free() -> None:
    journal = ContextJournal(
        (ModelMessage(role="system", content="rules"), ModelMessage(role="user", content="now"))
    )
    model = SummaryModel()
    projector = ContextProjector(max_chars=6_000, soft_chars=4_096)

    first = run(projector.project_async(history_sources(), journal, model))
    second = run(projector.project_async(history_sources(), journal, model))

    assert first.semantic_compacted is True
    assert first.messages[1].role == "system"
    assert "context_summary" in first.messages[1].content
    assert first.digest == second.digest
    assert len(model.requests) == 1
    assert model.requests[0].tools == ()
    assert model.requests[0].tool_choice == "none"
    assert model.requests[0].max_tokens == 1_536

    for index in range(4):
        journal.append(ModelMessage(role="user", content=f"new-{index} " + "u" * 900))
        journal.append(
            ModelMessage(role="assistant", content=f"result-{index} " + "a" * 900)
        )
    third = run(projector.project_async(history_sources(), journal, model))
    fourth = run(projector.project_async(history_sources(), journal, model))

    assert third.semantic_compacted is True
    assert third.generation == 2
    assert third.digest == fourth.digest
    assert len(model.requests) == 2


def test_semantic_compaction_never_replaces_the_active_user_task() -> None:
    task = "CURRENT TASK: preserve this exact request"
    journal = ContextJournal(
        (ModelMessage(role="system", content="rules"), ModelMessage(role="user", content=task))
    )
    for index in range(4):
        journal.append(
            ModelMessage(role="assistant", content=f"old-step-{index} " + "x" * 900)
        )
        journal.append(ModelMessage(role="user", content=f"step-{index}-feedback"))

    model = SummaryModel()
    projection = run(
        ContextProjector(max_chars=6_000, soft_chars=4_096).project_async(
            history_sources(content_size=500), journal, model
        )
    )

    assert sum(message.content == task for message in projection.messages) == 1
    assert projection.projected_chars <= 6_000
    assert model.requests
    assert all(task not in request.messages[-1].content for request in model.requests)


def test_semantic_compaction_can_reduce_history_and_old_live_steps_separately() -> None:
    task = "CURRENT TASK: keep me outside every summary"
    journal = ContextJournal(
        (ModelMessage(role="system", content="rules"), ModelMessage(role="user", content=task))
    )
    for index in range(6):
        journal.append(
            ModelMessage(role="assistant", content=f"old-step-{index} " + "x" * 1_100)
        )
        journal.append(ModelMessage(role="user", content=f"step-{index}-feedback"))

    model = SummaryModel()
    projection = run(
        ContextProjector(max_chars=5_000, soft_chars=4_096).project_async(
            history_sources(content_size=500), journal, model
        )
    )

    assert projection.projected_chars <= 5_000
    assert sum(message.content == task for message in projection.messages) == 1
    assert len(model.requests) == 2
    assert all(task not in request.messages[-1].content for request in model.requests)


def test_invalid_semantic_summary_falls_back_or_fails_closed() -> None:
    journal = ContextJournal(
        (ModelMessage(role="system", content="s" * 4_500), ModelMessage(role="user", content="now"))
    )
    projector = ContextProjector(max_chars=4_096, soft_chars=4_096)

    with pytest.raises(ContextBudgetError):
        run(
            projector.project_async(
                history_sources(content_size=100), journal, SummaryModel("bad")
            )
        )


def test_invalid_semantic_summary_uses_deterministic_fallback_when_it_fits() -> None:
    journal = ContextJournal(
        (ModelMessage(role="system", content="rules"), ModelMessage(role="user", content="task"))
    )
    for index in range(4):
        call = ToolCall(f"call-{index}", "workspace_read", {"path": f"file-{index}.py"})
        journal.append(ModelMessage(role="assistant", content="read", tool_calls=(call,)))
        journal.append(
            ModelMessage(
                role="tool",
                tool_call_id=call.id,
                content=json.dumps(
                    {"ok": True, "tool": "workspace_read", "result": {"content": "x" * 300}}
                ),
            )
        )

    projection = run(
        ContextProjector(max_chars=8_000, soft_chars=4_096).project_async(
            (), journal, SummaryModel("not-json"), force_semantic=True
        )
    )

    assert projection.semantic_fallback is True
    assert projection.projected_chars <= 8_000


def test_semantic_compaction_cancellation_propagates() -> None:
    class CancelledModel:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise asyncio.CancelledError

    journal = ContextJournal(
        (ModelMessage(role="system", content="rules"), ModelMessage(role="user", content="now"))
    )
    with pytest.raises(asyncio.CancelledError):
        run(
            ContextProjector(max_chars=6_000, soft_chars=4_096).project_async(
                history_sources(), journal, CancelledModel()
            )
        )


def test_historical_run_evidence_is_projected_without_tool_content(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path))
    run_record = store.create_run(session.id, "coding", "Inspect README", 4)
    store.add_message(session.id, "user", "Inspect README", run_id=run_record.id)
    store.append_event(
        session.id,
        run_record.id,
        0,
        "tool.completed",
        {
            "type": "tool.completed",
            "tool": "workspace_read",
            "content": "private source body",
            "metadata": {"path": "README.md", "ok": True},
        },
    )
    store.append_event(
        session.id,
        run_record.id,
        1,
        "run.completed",
        {
            "type": "run.completed",
            "metadata": {
                "evidence": {"observations": 1, "changed_files": []}
            },
        },
    )
    store.update_run(run_record.id, state="COMPLETED", answer="README inspected")
    store.add_message(
        session.id, "assistant", "README inspected", run_id=run_record.id
    )

    history = context_history_for_session(store, session)
    rendered = "\n".join(source.message.content for source in history)

    assert "historical_run_evidence" in rendered
    assert "README.md" in rendered
    assert "private source body" not in rendered
    assert history[-1].message.content == "README inspected"


def test_persistent_history_budget_keeps_complete_run_turns(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path))
    for index in range(3):
        run_record = store.create_run(session.id, "coding", f"task-{index}", 4)
        store.add_message(
            session.id,
            "user",
            f"task-{index}:" + "u" * 15_000,
            run_id=run_record.id,
        )
        store.update_run(run_record.id, state="COMPLETED", answer=f"answer-{index}")
        store.add_message(
            session.id,
            "assistant",
            f"answer-{index}:" + "a" * 8_000,
            run_id=run_record.id,
        )

    history = context_history_for_session(store, session)
    rendered = "\n".join(source.message.content for source in history)

    assert "task-0:" not in rendered
    assert "answer-0:" not in rendered
    assert "task-1:" in rendered
    assert "answer-1:" in rendered
    assert "task-2:" in rendered
    assert "answer-2:" in rendered
    assert [source.message.role for source in history] == [
        "user",
        "system",
        "assistant",
        "user",
        "system",
        "assistant",
    ]


def test_runtime_recovers_one_provider_context_overflow(tmp_path: Path) -> None:
    class OverflowModel:
        def __init__(self) -> None:
            self.task_calls = 0
            self.summary_calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.messages[0].content.startswith("Return only a JSON object"):
                self.summary_calls += 1
                return ModelResponse(content=SUMMARY)
            self.task_calls += 1
            if self.task_calls == 1:
                raise ModelContextLengthError("provider context exceeded")
            return ModelResponse(content="Recovered successfully.")

    model = OverflowModel()
    agent = CodingAgent(
        tmp_path,
        model,
        context_chars=20_000,
        history_sources=history_sources(content_size=400),
    )

    events = run(_collect(agent, "Inspect the project"))

    assert events[-1].type == "run.completed"
    assert model.task_calls == 2
    assert model.summary_calls == 1
    assert any(event.type == "context.compacted" for event in events)


async def _collect(agent: CodingAgent, task: str):
    return [event async for event in agent.run(task)]
