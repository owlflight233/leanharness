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

from leanharness.errors import (
    InvalidPermissionError,
    PlanConflictError,
    PlanNotFoundError,
    PlanStateError,
    SessionNotFoundError,
    StorageError,
)
from leanharness.planning.contracts import Plan, PlanState, PlanStep, PlanStepState
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
        kind: str = "chat",
        plan_id: str | None = None,
    ) -> MessageRecord:
        self.get_session(session_id)
        safe_content = self.redactor.text(content)
        message_id = str(uuid.uuid4())
        created_at = utc_now()
        row = self.connection.execute(
            """INSERT INTO messages(id, session_id, sequence, role, content, status, created_at, run_id, kind, plan_id)
               SELECT ?, ?, COALESCE(MAX(sequence), -1) + 1, ?, ?, ?, ?, ?, ?, ?
               FROM messages WHERE session_id=? RETURNING sequence""",
            (
                message_id,
                session_id,
                role,
                safe_content,
                status,
                created_at,
                run_id,
                kind,
                plan_id,
                session_id,
            ),
        ).fetchone()
        sequence = int(row["sequence"])
        message = MessageRecord(
            message_id, session_id, sequence, role, safe_content, status, created_at, run_id, kind, plan_id
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
                        "kind",
                        "plan_id",
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

    def get_run(self, run_id: str) -> RunRecord:
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StorageError("Run was not found")
        return RunRecord(
            *(row[key] for key in (
                "id", "session_id", "mode", "task", "state", "max_steps",
                "permission_mode", "answer", "error_code", "started_at", "finished_at",
            ))
        )

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload_json FROM run_events WHERE run_id=? ORDER BY sequence", (run_id,)
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def create_plan(
        self,
        session_id: str,
        *,
        title: str,
        task: str,
        source_markdown: str,
        steps: tuple[PlanStep, ...],
        state: PlanState = PlanState.AWAITING_CONFIRMATION,
    ) -> Plan:
        self.get_session(session_id)
        now = utc_now()
        plan = Plan(
            id=str(uuid.uuid4()),
            session_id=session_id,
            title=self.redactor.text(title),
            task=self.redactor.text(task),
            state=state,
            version=1,
            source_markdown=self.redactor.text(source_markdown),
            run_id=None,
            created_at=now,
            updated_at=now,
            steps=steps,
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO plans(id, session_id, title, task, state, version, source_markdown, run_id, created_at, updated_at, confirmed_at, finished_at, error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan.id,
                    plan.session_id,
                    plan.title,
                    plan.task,
                    plan.state.value,
                    plan.version,
                    plan.source_markdown,
                    None,
                    now,
                    now,
                    None,
                    None,
                    None,
                ),
            )
            self._insert_plan_steps(plan.id, steps)
        return plan

    def list_plans(self, session_id: str) -> list[Plan]:
        self.get_session(session_id)
        rows = self.connection.execute(
            "SELECT * FROM plans WHERE session_id=? ORDER BY updated_at DESC", (session_id,)
        ).fetchall()
        return [self._plan_row(row) for row in rows]

    def get_plan(self, plan_id: str) -> Plan:
        row = self.connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise PlanNotFoundError("Plan was not found")
        return self._plan_row(row)

    def update_plan(
        self,
        plan_id: str,
        *,
        version: int,
        title: str,
        source_markdown: str,
        steps: tuple[PlanStep, ...],
    ) -> Plan:
        current = self.get_plan(plan_id)
        if current.version != version:
            raise PlanConflictError("Plan version is stale")
        if current.state is not PlanState.AWAITING_CONFIRMATION:
            raise PlanStateError("Only an unconfirmed plan can be edited")
        now = utc_now()
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE plans SET title=?, source_markdown=?, version=version+1, updated_at=? WHERE id=? AND version=? AND state=?",
                (
                    self.redactor.text(title),
                    self.redactor.text(source_markdown),
                    now,
                    plan_id,
                    version,
                    PlanState.AWAITING_CONFIRMATION.value,
                ),
            )
            if cursor.rowcount != 1:
                raise PlanConflictError("Plan version is stale")
            self.connection.execute("DELETE FROM plan_steps WHERE plan_id=?", (plan_id,))
            self._insert_plan_steps(plan_id, steps)
        return self.get_plan(plan_id)

    def attach_plan_run(self, plan_id: str, run_id: str) -> Plan:
        self.get_plan(plan_id)
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE plans SET run_id=?, state=?, confirmed_at=COALESCE(confirmed_at, ?), updated_at=? WHERE id=?",
                (run_id, PlanState.RUNNING.value, now, now, plan_id),
            )
        return self.get_plan(plan_id)

    def update_plan_state(
        self,
        plan_id: str,
        state: PlanState,
        *,
        error_code: str | None = None,
    ) -> Plan:
        self.get_plan(plan_id)
        now = utc_now()
        finished = now if state in {
            PlanState.COMPLETED,
            PlanState.FAILED,
            PlanState.CANCELLED,
        } else None
        with self.connection:
            self.connection.execute(
                "UPDATE plans SET state=?, error_code=COALESCE(?, error_code), finished_at=COALESCE(?, finished_at), updated_at=? WHERE id=?",
                (state.value, error_code, finished, now, plan_id),
            )
        return self.get_plan(plan_id)

    def update_plan_step(
        self,
        step_id: str,
        state: PlanStepState,
        *,
        evidence: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> PlanStep:
        row = self.connection.execute(
            "SELECT * FROM plan_steps WHERE id=?", (step_id,)
        ).fetchone()
        if row is None:
            raise PlanNotFoundError("Plan step was not found")
        now = utc_now()
        started = now if state is PlanStepState.RUNNING else row["started_at"]
        finished = now if state in {
            PlanStepState.COMPLETED,
            PlanStepState.FAILED,
            PlanStepState.SKIPPED,
        } else row["finished_at"]
        with self.connection:
            self.connection.execute(
                "UPDATE plan_steps SET state=?, evidence_json=?, error_code=?, started_at=?, finished_at=? WHERE id=?",
                (
                    state.value,
                    json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
                    if evidence is not None
                    else row["evidence_json"],
                    error_code,
                    started,
                    finished,
                    step_id,
                ),
            )
            self.connection.execute(
                "UPDATE plans SET updated_at=? WHERE id=?", (now, row["plan_id"])
            )
        plan = self.get_plan(row["plan_id"])
        return next(step for step in plan.steps if step.id == step_id)

    def delete_plan(self, plan_id: str) -> None:
        self.get_plan(plan_id)
        with self.connection:
            self.connection.execute("DELETE FROM plans WHERE id=?", (plan_id,))

    def _insert_plan_steps(self, plan_id: str, steps: tuple[PlanStep, ...]) -> None:
        self.connection.executemany(
            "INSERT INTO plan_steps(id, plan_id, sequence, title, instruction, enabled, state, evidence_json, error_code, started_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    step.id,
                    plan_id,
                    step.sequence,
                    self.redactor.text(step.title),
                    self.redactor.text(step.instruction),
                    int(step.enabled),
                    step.state.value,
                    json.dumps(step.evidence, ensure_ascii=False, separators=(",", ":"))
                    if step.evidence is not None
                    else None,
                    step.error_code,
                    step.started_at,
                    step.finished_at,
                )
                for step in steps
            ],
        )

    def _plan_row(self, row: sqlite3.Row) -> Plan:
        steps_rows = self.connection.execute(
            "SELECT * FROM plan_steps WHERE plan_id=? ORDER BY sequence", (row["id"],)
        ).fetchall()
        steps = tuple(
            PlanStep(
                id=step["id"],
                sequence=step["sequence"],
                title=step["title"],
                instruction=step["instruction"],
                enabled=bool(step["enabled"]),
                state=PlanStepState(step["state"]),
                evidence=json.loads(step["evidence_json"])
                if step["evidence_json"]
                else None,
                error_code=step["error_code"],
                started_at=step["started_at"],
                finished_at=step["finished_at"],
            )
            for step in steps_rows
        )
        return Plan(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            task=row["task"],
            state=PlanState(row["state"]),
            version=row["version"],
            source_markdown=row["source_markdown"],
            run_id=row["run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            confirmed_at=row["confirmed_at"],
            finished_at=row["finished_at"],
            error_code=row["error_code"],
            steps=steps,
        )

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
            self.connection.execute(
                "UPDATE plans SET state='PAUSED', updated_at=? WHERE state='RUNNING'",
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
