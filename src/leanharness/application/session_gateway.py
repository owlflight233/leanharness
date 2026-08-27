"""Application services for persistent projects, sessions, and public records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanharness.models import ModelEvent
from leanharness.runtime.events import RuntimeEvent
from leanharness.storage import LocalStore, ProjectRecord, RunRecord, SessionRecord


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
        "answer": run.answer,
        "error_code": run.error_code,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def apply_first_task_title(store: LocalStore, session: SessionRecord, task: str) -> SessionRecord:
    """Name an untouched default session from its first submitted task."""
    if session.title != "新会话":
        return session
    normalized = " ".join(task.strip().split())[:40]
    return store.update_session(session.id, title=normalized or "新会话")


def session_detail(store: LocalStore, session_id: str) -> dict[str, object]:
    session = store.get_session(session_id)
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
            }
            for message in store.list_messages(session_id)
        ],
        "runs": [run_to_dict(run) for run in store.list_runs(session_id)],
    }


def persist_runtime_event(
    store: LocalStore, session: SessionRecord, run: RunRecord, event: RuntimeEvent
) -> None:
    payload = event.to_dict()
    store.append_event(session.id, run.id, event.sequence, event.type, payload)
    if event.type == "assistant.progress" and event.summary:
        store.add_message(session.id, "progress", event.summary)
    elif event.type in {"run.completed", "run.incomplete", "run.failed", "run.cancelled"}:
        content = event.answer or event.summary or event.error_message or event.type
        status = {
            "run.completed": "complete",
            "run.incomplete": "incomplete",
            "run.failed": "error",
            "run.cancelled": "cancelled",
        }[event.type]
        store.add_message(session.id, "assistant", content, status)
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
            session.id, "assistant", event.error_message or "Model request failed", "error"
        )
        store.update_run(run.id, state="FAILED", error_code=event.error_code)
    elif event.type == "turn.completed":
        store.update_run(run.id, state="COMPLETED")
