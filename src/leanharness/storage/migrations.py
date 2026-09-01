"""Forward-only SQLite schema migrations."""

# SQL statements stay on one line so the migration schema is easy to compare.
# ruff: noqa: E501

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 6


def apply_migrations(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        current = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()["version"]
        if current < 1:
            connection.executescript(
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
        if current < 2:
            connection.executescript(
                """
                ALTER TABLE sessions ADD COLUMN language TEXT;
                ALTER TABLE messages ADD COLUMN run_id TEXT REFERENCES runs(id) ON DELETE SET NULL;
                ALTER TABLE runs ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'inspect';
                CREATE TABLE approvals(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL, requested_at TEXT NOT NULL, decided_at TEXT);
                CREATE INDEX messages_run ON messages(run_id, sequence);
                CREATE INDEX approvals_run ON approvals(run_id, requested_at);
                INSERT INTO schema_migrations(version, applied_at) VALUES(2, CURRENT_TIMESTAMP);
                """
            )
        if current < 3:
            connection.executescript(
                """
                CREATE TABLE plans(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    task TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_markdown TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    finished_at TEXT,
                    error_code TEXT
                );
                CREATE TABLE plan_steps(
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    evidence_json TEXT,
                    error_code TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(plan_id, sequence)
                );
                CREATE INDEX plans_session_updated ON plans(session_id, updated_at DESC);
                CREATE INDEX plan_steps_order ON plan_steps(plan_id, sequence);
                INSERT INTO schema_migrations(version, applied_at) VALUES(3, CURRENT_TIMESTAMP);
                """
            )
        if current < 4:
            connection.executescript(
                """
                ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat';
                ALTER TABLE messages ADD COLUMN plan_id TEXT REFERENCES plans(id) ON DELETE SET NULL;
                CREATE INDEX messages_plan ON messages(plan_id, sequence);
                INSERT INTO schema_migrations(version, applied_at) VALUES(4, CURRENT_TIMESTAMP);
                """
            )
        if current < 5:
            connection.executescript(
                """
                CREATE TABLE attachments(
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX attachments_session ON attachments(session_id, created_at);
                CREATE INDEX attachments_message ON attachments(message_id);
                INSERT INTO schema_migrations(version, applied_at) VALUES(5, CURRENT_TIMESTAMP);
                """
            )
        if current < 6:
            connection.executescript(
                """
                CREATE TABLE plugins(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    description TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    install_path TEXT NOT NULL UNIQUE,
                    entrypoint_json TEXT NOT NULL,
                    tools_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    installed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX plugins_enabled ON plugins(enabled, id);
                INSERT INTO schema_migrations(version, applied_at) VALUES(6, CURRENT_TIMESTAMP);
                """
            )
