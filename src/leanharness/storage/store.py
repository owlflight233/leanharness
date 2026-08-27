"""Local SQLite state and redacted JSONL trace persistence."""

# SQL statements are intentionally kept readable as schema definitions.
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import platform
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanharness.errors import InvalidPermissionError, SessionNotFoundError, StorageError

PERMISSION_MODES = frozenset({"inspect", "approve", "unrestricted"})
SCHEMA_VERSION = 1


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


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    project_id: str
    title: str
    permission_mode: str
    created_at: str
    updated_at: str
    last_run_state: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    root_path: str
    permission_mode: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: str
    session_id: str
    sequence: int
    role: str
    content: str
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    session_id: str
    mode: str
    task: str
    state: str
    max_steps: int
    answer: str | None
    error_code: str | None
    started_at: str
    finished_at: str | None


class LocalStore:
    """Small synchronous repository for local, user-owned application state."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = (data_dir or default_data_dir()).expanduser().resolve()
        self.trace_dir = self.data_dir / "traces"
        self.db_path = self.data_dir / "leanharness.sqlite3"
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
            self._migrate()
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
        self, project: ProjectRecord, *, title: str = "新会话", permission_mode: str | None = None
    ) -> SessionRecord:
        mode = permission_mode or project.permission_mode
        _validate_permission(mode)
        now = utc_now()
        session_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO sessions(id, project_id, title, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (session_id, project.id, title or "新会话", mode, now, now),
        )
        self.connection.commit()
        return SessionRecord(session_id, project.id, title or "新会话", mode, now, now)

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
        self, session_id: str, *, title: str | None = None, permission_mode: str | None = None
    ) -> SessionRecord:
        self.get_session(session_id)
        if permission_mode is not None:
            _validate_permission(permission_mode)
        now = utc_now()
        self.connection.execute(
            "UPDATE sessions SET title = COALESCE(?, title), permission_mode = COALESCE(?, permission_mode), updated_at = ? WHERE id = ?",
            (title, permission_mode, now, session_id),
        )
        self.connection.commit()
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> None:
        session = self.get_session(session_id)
        run_ids = [
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM runs WHERE session_id=?", (session_id,)
            )
        ]
        with self.connection:
            self.connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        paths = [self._trace_path(session.project_id, session_id, run_id) for run_id in run_ids]
        for path in paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                raise StorageError("Session trace could not be deleted") from exc

    def add_message(
        self, session_id: str, role: str, content: str, status: str = "complete"
    ) -> MessageRecord:
        self.get_session(session_id)
        row = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()
        sequence = int(row["next_sequence"])
        message = MessageRecord(
            str(uuid.uuid4()), session_id, sequence, role, content, status, utc_now()
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO messages(id, session_id, sequence, role, content, status, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    message.id,
                    message.session_id,
                    message.sequence,
                    message.role,
                    message.content,
                    message.status,
                    message.created_at,
                ),
            )
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
                    )
                )
            )
            for row in rows
        ]

    def create_run(self, session_id: str, mode: str, task: str, max_steps: int) -> RunRecord:
        self.get_session(session_id)
        now = utc_now()
        run = RunRecord(
            str(uuid.uuid4()), session_id, mode, task, "CREATED", max_steps, None, None, now, None
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs(id, session_id, mode, task, state, max_steps, answer, error_code, started_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.session_id,
                    run.mode,
                    run.task,
                    run.state,
                    run.max_steps,
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
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET state=?, answer=COALESCE(?,answer), error_code=COALESCE(?,error_code), finished_at=COALESCE(?,finished_at) WHERE id=?",
                (state, answer, error_code, now, run_id),
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
        safe = redact_payload(payload)
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

    def trace_path(self, session_id: str, run_id: str) -> Path:
        session = self.get_session(session_id)
        return self._trace_path(session.project_id, session_id, run_id)

    def _trace_path(self, project_id: str, session_id: str, run_id: str) -> Path:
        return self.trace_dir / project_id / session_id / f"{run_id}.jsonl"

    def _migrate(self) -> None:
        with self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()["version"]
            if current < 1:
                self.connection.executescript(
                    """
                    CREATE TABLE projects(id TEXT PRIMARY KEY, root_path TEXT NOT NULL UNIQUE, permission_mode TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE sessions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, title TEXT NOT NULL, permission_mode TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, sequence));
                    CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, mode TEXT NOT NULL, task TEXT NOT NULL, state TEXT NOT NULL, max_steps INTEGER NOT NULL, answer TEXT, error_code TEXT, started_at TEXT NOT NULL, finished_at TEXT);
                    CREATE TABLE run_events(run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, sequence INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id, sequence));
                    CREATE INDEX sessions_project_updated ON sessions(project_id, updated_at DESC);
                    INSERT INTO schema_migrations(version, applied_at) VALUES(1, CURRENT_TIMESTAMP);
                    """
                )

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
            row["created_at"],
            row["updated_at"],
            row["last_run_state"],
        )


def _validate_permission(value: str) -> None:
    if value not in PERMISSION_MODES:
        raise InvalidPermissionError("Permission mode is invalid")


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep public event metadata while removing secrets and raw tool content."""
    return _redact_mapping(payload, event_type=str(payload.get("type", "")))


def _redact_mapping(value: dict[str, Any], *, event_type: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    forbidden = {"api_key", "authorization", "headers", "environment", "env", "cookie"}
    for key, item in value.items():
        if key.casefold() in forbidden:
            continue
        if key == "content" and event_type.startswith("tool"):
            result[key] = "[tool result redacted]"
        elif isinstance(item, dict):
            result[key] = _redact_mapping(item, event_type=event_type)
        elif isinstance(item, list):
            result[key] = [
                _redact_mapping(entry, event_type=event_type) if isinstance(entry, dict) else entry
                for entry in item[:100]
            ]
        elif key in {"content", "answer", "summary"} and isinstance(item, str):
            result[key] = item[:64_000] + ("...[truncated]" if len(item) > 64_000 else "")
        else:
            result[key] = item
    return result
