"""Local SQLite state and redacted JSONL trace persistence."""

# SQL statements are intentionally kept readable as schema definitions.
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanharness.errors import InvalidPermissionError, SessionNotFoundError, StorageError
from leanharness.storage.migrations import apply_migrations
from leanharness.storage.records import (
    ApprovalRecord,
    MessageRecord,
    ProjectRecord,
    RunRecord,
    SessionRecord,
)
from leanharness.storage.redaction import TraceRedactor

PERMISSION_MODES = frozenset({"inspect", "approve", "unrestricted"})
_TRACE_WRITE_LOCK = threading.Lock()


def default_data_dir() -> Path:
    override = os.environ.get("LEANHARNESS_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        root = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(root) / "LeanHarness"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "LeanHarness"
    root = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(root) / "leanharness"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LocalStore:
    """Small synchronous repository for local, user-owned application state."""

    def __init__(
        self, data_dir: Path | None = None, *, redactor: TraceRedactor | None = None
    ) -> None:
        self.data_dir = (data_dir or default_data_dir()).expanduser().resolve()
        self.trace_dir = self.data_dir / "traces"
        self.db_path = self.data_dir / "leanharness.sqlite3"
        self.redactor = redactor or TraceRedactor.from_environment()
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> LocalStore:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def open(self) -> None:
        if self._connection is not None:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            self._connection = connection
            apply_migrations(connection)
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise StorageError("Local session storage could not be opened") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.open()
        assert self._connection is not None
        return self._connection

    def ensure_project(self, root_path: Path, permission_mode: str = "inspect") -> ProjectRecord:
        _validate_permission(permission_mode)
        root = root_path.resolve(strict=True)
        now = utc_now()
        row = self.connection.execute(
            "SELECT * FROM projects WHERE root_path = ?", (str(root),)
        ).fetchone()
        if row:
            self.connection.execute(
                "UPDATE projects SET permission_mode = ?, updated_at = ? WHERE id = ?",
                (permission_mode, now, row["id"]),
            )
            self.connection.commit()
            return self._project_row(row, permission_mode=permission_mode, updated_at=now)
        project_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO projects(id, root_path, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?)",
            (project_id, str(root), permission_mode, now, now),
        )
        self.connection.commit()
        return ProjectRecord(project_id, str(root), permission_mode, now, now)

    def create_session(
        self,
        project: ProjectRecord,
        *,
        title: str = "新会话",
        permission_mode: str | None = None,
        language: str | None = None,
    ) -> SessionRecord:
        mode = permission_mode or project.permission_mode
        _validate_permission(mode)
        now = utc_now()
        session_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO sessions(id, project_id, title, permission_mode, language, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                session_id,
                project.id,
                self.redactor.text(title or "新会话"),
                mode,
                language,
                now,
                now,
            ),
        )
        self.connection.commit()
        return SessionRecord(
            session_id,
            project.id,
            self.redactor.text(title or "新会话"),
            mode,
            language,
            now,
            now,
        )

    def list_sessions(self, project: ProjectRecord) -> list[SessionRecord]:
        rows = self.connection.execute(
            """SELECT s.*, r.state AS last_run_state FROM sessions s
               LEFT JOIN runs r ON r.id = (SELECT id FROM runs WHERE session_id=s.id ORDER BY started_at DESC LIMIT 1)
               WHERE s.project_id = ? ORDER BY s.updated_at DESC""",
            (project.id,),
        ).fetchall()
        return [self._session_row(row) for row in rows]

    def get_session(self, session_id: str) -> SessionRecord:
        row = self.connection.execute(
            """SELECT s.*, r.state AS last_run_state FROM sessions s
               LEFT JOIN runs r ON r.id = (SELECT id FROM runs WHERE session_id=s.id ORDER BY started_at DESC LIMIT 1)
               WHERE s.id = ?""",
            (session_id,),
        ).fetchone()
        if not row:
            raise SessionNotFoundError("Session was not found")
        return self._session_row(row)

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        permission_mode: str | None = None,
        language: str | None = None,
    ) -> SessionRecord:
        self.get_session(session_id)
        if permission_mode is not None:
            _validate_permission(permission_mode)
        now = utc_now()
        self.connection.execute(
            "UPDATE sessions SET title = COALESCE(?, title), permission_mode = COALESCE(?, permission_mode), language = COALESCE(?, language), updated_at = ? WHERE id = ?",
            (
                self.redactor.text(title) if title is not None else None,
                permission_mode,
                language,
                now,
                session_id,
            ),
        )
        self.connection.commit()
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> None:
        session = self.get_session(session_id)
        with self.connection:
            self.connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        trace_session_dir = self.trace_dir / session.project_id / session_id
        try:
            if trace_session_dir.exists():
                shutil.rmtree(trace_session_dir)
        except OSError as exc:
            raise StorageError("Session trace could not be deleted") from exc

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        status: str = "complete",
        *,
        run_id: str | None = None,
    ) -> MessageRecord:
        self.get_session(session_id)
        safe_content = self.redactor.text(content)
        message_id = str(uuid.uuid4())
        created_at = utc_now()
        row = self.connection.execute(
            """INSERT INTO messages(id, session_id, sequence, role, content, status, created_at, run_id)
               SELECT ?, ?, COALESCE(MAX(sequence), -1) + 1, ?, ?, ?, ?, ?
               FROM messages WHERE session_id=? RETURNING sequence""",
            (
                message_id,
                session_id,
                role,
                safe_content,
                status,
                created_at,
                run_id,
                session_id,
            ),
        ).fetchone()
        sequence = int(row["sequence"])
        message = MessageRecord(
            message_id, session_id, sequence, role, safe_content, status, created_at, run_id
        )
        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?", (message.created_at, session_id)
            )
        return message

    def list_messages(self, session_id: str) -> list[MessageRecord]:
        self.get_session(session_id)
        rows = self.connection.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY sequence", (session_id,)
        ).fetchall()
        return [
            MessageRecord(
                *(
                    row[key]
                    for key in (
                        "id",
                        "session_id",
                        "sequence",
                        "role",
                        "content",
                        "status",
                        "created_at",
                        "run_id",
                    )
                )
            )
            for row in rows
        ]

    def create_run(
        self,
        session_id: str,
        mode: str,
        task: str,
        max_steps: int,
        *,
        permission_mode: str | None = None,
    ) -> RunRecord:
        self.get_session(session_id)
        now = utc_now()
        safe_task = self.redactor.text(task)
        selected_permission = permission_mode or self.get_session(session_id).permission_mode
        _validate_permission(selected_permission)
        run = RunRecord(
            str(uuid.uuid4()),
            session_id,
            mode,
            safe_task,
            "CREATED",
            max_steps,
            selected_permission,
            None,
            None,
            now,
            None,
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs(id, session_id, mode, task, state, max_steps, permission_mode, answer, error_code, started_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.session_id,
                    run.mode,
                    run.task,
                    run.state,
                    run.max_steps,
                    run.permission_mode,
                    run.answer,
                    run.error_code,
                    run.started_at,
                    run.finished_at,
                ),
            )
        return run

    def update_run(
        self, run_id: str, *, state: str, answer: str | None = None, error_code: str | None = None
    ) -> RunRecord:
        now = utc_now() if state in {"COMPLETED", "EXHAUSTED", "FAILED", "CANCELLED"} else None
        safe_answer = self.redactor.text(answer) if answer is not None else None
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET state=?, answer=COALESCE(?,answer), error_code=COALESCE(?,error_code), finished_at=COALESCE(?,finished_at) WHERE id=?",
                (state, safe_answer, error_code, now, run_id),
            )
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise StorageError("Run was not found")
        return RunRecord(
            *(
                row[key]
                for key in (
                    "id",
                    "session_id",
                    "mode",
                    "task",
                    "state",
                    "max_steps",
                    "permission_mode",
                    "answer",
                    "error_code",
                    "started_at",
                    "finished_at",
                )
            )
        )

    def append_event(
        self, session_id: str, run_id: str, sequence: int, event_type: str, payload: dict[str, Any]
    ) -> None:
        safe = self.redactor.payload(payload)
        with self.connection:
            self.connection.execute(
                "INSERT INTO run_events(run_id, sequence, event_type, payload_json, created_at) VALUES(?,?,?,?,?)",
                (
                    run_id,
                    sequence,
                    event_type,
                    json.dumps(safe, ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                ),
            )
        try:
            path = self.trace_path(session_id, run_id)
            with _TRACE_WRITE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {"sequence": sequence, "type": event_type, **safe},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
        except OSError as exc:
            with self.connection:
                self.connection.execute(
                    "DELETE FROM run_events WHERE run_id=? AND sequence=?", (run_id, sequence)
                )
            raise StorageError("Run trace could not be written") from exc

    def list_runs(self, session_id: str) -> list[RunRecord]:
        self.get_session(session_id)
        rows = self.connection.execute(
            "SELECT * FROM runs WHERE session_id=? ORDER BY started_at", (session_id,)
        ).fetchall()
        return [
            RunRecord(
                *(
                    row[key]
                    for key in (
                        "id",
                        "session_id",
                        "mode",
                        "task",
                        "state",
                        "max_steps",
                        "permission_mode",
                        "answer",
                        "error_code",
                        "started_at",
                        "finished_at",
                    )
                )
            )
            for row in rows
        ]

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload_json FROM run_events WHERE run_id=? ORDER BY sequence", (run_id,)
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def create_approval(
        self,
        approval_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        request: dict[str, Any],
    ) -> ApprovalRecord:
        safe_request = self.redactor.payload(request)
        safe_request.pop("preview", None)
        requested_at = utc_now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO approvals(id, run_id, tool_call_id, tool_name, request_json, state, requested_at, decided_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    approval_id,
                    run_id,
                    tool_call_id,
                    tool_name,
                    json.dumps(safe_request, ensure_ascii=False, separators=(",", ":")),
                    "PENDING",
                    requested_at,
                ),
            )
        return ApprovalRecord(
            approval_id,
            run_id,
            tool_call_id,
            tool_name,
            safe_request,
            "PENDING",
            requested_at,
            None,
        )

    def resolve_approval(self, approval_id: str, decision: str) -> ApprovalRecord:
        state = {"approve": "APPROVED", "reject": "REJECTED"}.get(decision)
        if state is None:
            raise StorageError("Approval decision is invalid")
        decided_at = utc_now()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE approvals SET state=?, decided_at=? WHERE id=? AND state='PENDING'",
                (state, decided_at, approval_id),
            )
        if cursor.rowcount != 1:
            raise StorageError("Approval was not pending")
        return self.get_approval(approval_id)

    def expire_approval(self, approval_id: str) -> ApprovalRecord:
        decided_at = utc_now()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE approvals SET state='EXPIRED', decided_at=? "
                "WHERE id=? AND state='PENDING'",
                (decided_at, approval_id),
            )
        if cursor.rowcount != 1:
            raise StorageError("Approval was not pending")
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        row = self.connection.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise StorageError("Approval was not found")
        return ApprovalRecord(
            row["id"],
            row["run_id"],
            row["tool_call_id"],
            row["tool_name"],
            json.loads(row["request_json"]),
            row["state"],
            row["requested_at"],
            row["decided_at"],
        )

    def interrupt_active_runs(self) -> int:
        """Mark process-local runs that cannot be resumed after restart as failed."""

        now = utc_now()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE runs SET state='FAILED', error_code='RUN_INTERRUPTED', finished_at=? WHERE state IN ('CREATED','PREPARING','REQUESTING_MODEL','INTERPRETING','EXECUTING_TOOL','WAITING_APPROVAL')",
                (now,),
            )
            self.connection.execute(
                "UPDATE approvals SET state='EXPIRED', decided_at=? WHERE state='PENDING'",
                (now,),
            )
        return cursor.rowcount

    def trace_path(self, session_id: str, run_id: str) -> Path:
        session = self.get_session(session_id)
        return self._trace_path(session.project_id, session_id, run_id)

    def _trace_path(self, project_id: str, session_id: str, run_id: str) -> Path:
        return self.trace_dir / project_id / session_id / f"{run_id}.jsonl"


    @staticmethod
    def _project_row(
        row: sqlite3.Row, *, permission_mode: str | None = None, updated_at: str | None = None
    ) -> ProjectRecord:
        return ProjectRecord(
            row["id"],
            row["root_path"],
            permission_mode or row["permission_mode"],
            row["created_at"],
            updated_at or row["updated_at"],
        )

    @staticmethod
    def _session_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            row["id"],
            row["project_id"],
            row["title"],
            row["permission_mode"],
            row["language"],
            row["created_at"],
            row["updated_at"],
            row["last_run_state"],
        )


def _validate_permission(value: str) -> None:
    if value not in PERMISSION_MODES:
        raise InvalidPermissionError("Permission mode is invalid")
