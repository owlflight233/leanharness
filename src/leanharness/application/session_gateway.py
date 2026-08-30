"""Application services for persistent projects, sessions, and public records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanharness.application.language import detect_session_language
from leanharness.application.plan_gateway import plan_to_dict
from leanharness.models import ModelEvent, ModelMessage
from leanharness.runtime import ContinuationContext
from leanharness.runtime.events import RuntimeEvent
from leanharness.storage import LocalStore, MessageRecord, ProjectRecord, RunRecord, SessionRecord

MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_CHARS = 32_000


def ensure_session(
    store: LocalStore,
    workspace: Path,
    session_id: str | None = None,
    *,
    permission_mode: str = "inspect",
) -> tuple[ProjectRecord, SessionRecord]:
    project = store.ensure_project(workspace, permission_mode=permission_mode)
    if session_id is None:
        return project, store.create_session(project, permission_mode=permission_mode)
    session = store.get_session(session_id)
    if session.project_id != project.id:
        from leanharness.errors import SessionNotFoundError

        raise SessionNotFoundError("Session does not belong to the current workspace")
    return project, session


def session_to_dict(session: SessionRecord) -> dict[str, object]:
    return {
        "id": session.id,
        "project_id": session.project_id,
        "title": session.title,
        "permission_mode": session.permission_mode,
        "language": session.language,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "last_run_state": session.last_run_state,
    }


def run_to_dict(run: RunRecord) -> dict[str, object]:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "mode": run.mode,
        "task": run.task,
        "state": run.state,
        "max_steps": run.max_steps,
        "permission_mode": run.permission_mode,
        "answer": run.answer,
        "error_code": run.error_code,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def apply_first_task_title(store: LocalStore, session: SessionRecord, task: str) -> SessionRecord:
    """Name an untouched session and lock its default response language."""
    title = None
    if session.title == "新会话":
        title = " ".join(task.strip().split())[:40] or "新会话"
    language = detect_session_language(task) if session.language is None else None
    if title is None and language is None:
        return session
    return store.update_session(session.id, title=title, language=language)


def session_detail(store: LocalStore, session_id: str) -> dict[str, object]:
    session = store.get_session(session_id)
    plans = []
    for plan in store.list_plans(session_id):
        permission = session.permission_mode
        if plan.run_id:
            permission = store.get_run(plan.run_id).permission_mode
        plans.append(plan_to_dict(plan, execution_permission_mode=permission))
    runs = []
    for run in store.list_runs(session_id):
        summary = run_to_dict(run)
        events = store.list_events(run.id)
        summary["trace"] = [
            {
                key: event[key]
                for key in (
                    "sequence",
                    "type",
                    "run_id",
                    "plan_id",
                    "title",
                    "state",
                    "step_count",
                    "step",
                    "tool",
                    "summary",
                    "error",
                    "metadata",
                    "usage",
                )
                if key in event
            }
            for event in events
        ]
        runs.append(summary)
    return {
        "session": session_to_dict(session),
        "messages": [
            {
                "id": message.id,
                "sequence": message.sequence,
                "role": message.role,
                "content": message.content,
                "status": message.status,
                "created_at": message.created_at,
                "run_id": message.run_id,
                "kind": message.kind,
                "plan_id": message.plan_id,
            }
            for message in store.list_messages(session_id)
        ],
        "runs": runs,
        "plans": plans,
    }


def continuation_for_session(
    store: LocalStore,
    session: SessionRecord,
) -> ContinuationContext | None:
    """Return a bounded public capsule for the immediately preceding terminal run."""

    runs = store.list_runs(session.id)
    terminal = next(
        (
            run
            for run in reversed(runs)
            if run.state in {"COMPLETED", "EXHAUSTED", "FAILED", "CANCELLED"}
        ),
        None,
    )
    if terminal is None:
        return None
    events = store.list_events(terminal.id)
    terminal_event = next(
        (
            event
            for event in reversed(events)
            if event.get("type")
            in {"run.completed", "run.incomplete", "run.failed", "run.cancelled"}
        ),
        {},
    )
    metadata = terminal_event.get("metadata")
    evidence = metadata.get("evidence") if isinstance(metadata, dict) else None
    changed = evidence.get("changed_files") if isinstance(evidence, dict) else None
    changed_files = (
        tuple(str(path) for path in changed[:50]) if isinstance(changed, list) else ()
    )
    reason = terminal.error_code
    if reason is None and isinstance(metadata, dict):
        candidate = metadata.get("incomplete_reason")
        reason = str(candidate) if candidate else None
    return ContinuationContext(
        previous_task=terminal.task,
        previous_state=terminal.state,
        changed_files=changed_files,
        incomplete_reason=reason,
        permission_mode=session.permission_mode,
    )


def history_for_session(
    store: LocalStore,
    session: SessionRecord,
    *,
    exclude_run_id: str | None = None,
) -> tuple[ModelMessage, ...]:
    """Return bounded public conversation history for a new model request.

    Persisted tool events and progress notes are intentionally excluded. The
    current run is also excluded because its user message is appended by the
    runtime itself.
    """

    candidates = [
        message
        for message in store.list_messages(session.id)
        if (exclude_run_id is None or message.run_id != exclude_run_id)
        and message.role in {"user", "assistant"}
        and message.kind == "chat"
        and message.content.strip()
    ]
    selected: list[MessageRecord] = []
    size = 0
    for message in reversed(candidates):
        content = message.content.strip()
        cost = len(content) + 64
        if selected and (
            len(selected) >= MAX_HISTORY_MESSAGES or size + cost > MAX_HISTORY_CHARS
        ):
            break
        if not selected and cost > MAX_HISTORY_CHARS:
            content = content[:MAX_HISTORY_CHARS]
            cost = len(content) + 64
        selected.append(
            MessageRecord(
                message.id,
                message.session_id,
                message.sequence,
                message.role,
                content,
                message.status,
                message.created_at,
                message.run_id,
            )
        )
        size += cost
    selected.reverse()
    return tuple(ModelMessage(role=item.role, content=item.content) for item in selected)


def persist_runtime_event(
    store: LocalStore, session: SessionRecord, run: RunRecord, event: RuntimeEvent
) -> None:
    payload = event.to_dict()
    store.append_event(session.id, run.id, event.sequence, event.type, payload)
    if event.type in {"run.completed", "run.incomplete", "run.failed", "run.cancelled"}:
        content = event.answer or event.summary or event.error_message or event.type
        status = {
            "run.completed": "complete",
            "run.incomplete": "incomplete",
            "run.failed": "error",
            "run.cancelled": "cancelled",
        }[event.type]
        store.add_message(session.id, "assistant", content, status, run_id=run.id)
        store.update_run(
            run.id,
            state={
                "run.completed": "COMPLETED",
                "run.incomplete": "EXHAUSTED",
                "run.failed": "FAILED",
                "run.cancelled": "CANCELLED",
            }[event.type],
            answer=event.answer,
            error_code=event.error_code,
        )


def persist_model_event(
    store: LocalStore, session: SessionRecord, run: RunRecord, event: ModelEvent
) -> None:
    payload: dict[str, Any] = event.to_dict()
    store.append_event(session.id, run.id, event.sequence, event.type, payload)
    if event.type == "content.delta" and event.content:
        # Chat content is assembled in the final message by the caller.
        return
    if event.type == "turn.failed":
        store.add_message(
            session.id,
            "assistant",
            event.error_message or "Model request failed",
            "error",
            run_id=run.id,
        )
        store.update_run(run.id, state="FAILED", error_code=event.error_code)
    elif event.type == "turn.completed":
        store.update_run(run.id, state="COMPLETED")


def persist_stream_cancellation(
    store: LocalStore,
    session: SessionRecord,
    run: RunRecord,
    *,
    sequence: int,
    mode: str,
    partial_answer: str | None = None,
) -> None:
    """Record cancellation when a stream consumer disconnects before a terminal event."""

    event_type = "turn.cancelled" if mode == "chat" else "run.cancelled"
    summary = "Generation cancelled" if mode == "chat" else "Run cancelled"
    store.append_event(
        session.id,
        run.id,
        sequence,
        event_type,
        {"type": event_type, "sequence": sequence, "run_id": run.id, "summary": summary},
    )
    store.add_message(
        session.id,
        "assistant",
        partial_answer or summary,
        "cancelled",
        run_id=run.id,
    )
    store.update_run(run.id, state="CANCELLED", answer=partial_answer)
