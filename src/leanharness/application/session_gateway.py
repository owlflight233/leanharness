"""Application services for persistent projects, sessions, and public records."""

from __future__ import annotations

from pathlib import Path

from leanharness.application.language import detect_session_language
from leanharness.application.plan_gateway import plan_to_dict
from leanharness.models import ModelMessage
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


def history_for_session(
    store: LocalStore,
    session: SessionRecord,
    *,
    exclude_run_id: str | None = None,
) -> tuple[ModelMessage, ...]:
    """Return bounded public conversation history for a new model request.

    Persisted tool events and progress notes are intentionally excluded. Public
    user/assistant messages are the semantic continuity supplied to the model;
    the current run is excluded because its user message is appended by Runtime.
    """

    candidates = [
        message
        for message in store.list_messages(session.id)
        if (exclude_run_id is None or message.run_id != exclude_run_id)
        and message.role in {"user", "assistant"}
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
