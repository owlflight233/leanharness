from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from leanharness.errors import InvalidPermissionError, SessionNotFoundError
from leanharness.storage import LocalStore, TraceRedactor, default_data_dir, redact_payload

# Migration fixtures intentionally keep their SQL schema definitions readable.
# ruff: noqa: E501


def test_store_migrates_and_recovers_sessions(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    project = store.ensure_project(tmp_path)
    session = store.create_session(project, title="分析仓库")
    store.add_message(session.id, "user", "请检查")
    run = store.create_run(session.id, "inspect", "请检查", 4)
    store.append_event(
        session.id, run.id, 0, "run.started", {"type": "run.started", "summary": "started"}
    )
    store.update_run(run.id, state="COMPLETED", answer="完成")
    store.close()

    with LocalStore(tmp_path / "data") as reopened:
        project_again = reopened.ensure_project(tmp_path)
        sessions = reopened.list_sessions(project_again)
        assert sessions[0].title == "分析仓库"
        assert sessions[0].last_run_state == "COMPLETED"
        assert reopened.list_messages(session.id)[0].content == "请检查"
        assert reopened.list_events(run.id)[0]["summary"] == "started"


def test_delete_session_cascades_and_removes_trace(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    project = store.ensure_project(tmp_path)
    session = store.create_session(project)
    run = store.create_run(session.id, "inspect", "task", 2)
    store.append_event(session.id, run.id, 0, "run.started", {"type": "run.started"})
    trace = store.trace_path(session.id, run.id)
    assert trace.exists()

    store.delete_session(session.id)

    with pytest.raises(SessionNotFoundError):
        store.get_session(session.id)
    assert not trace.exists()


def test_trace_redaction_never_persists_secret_or_tool_content(tmp_path: Path) -> None:
    payload = {
        "type": "tool.completed",
        "content": "private source text",
        "api_key": "secret-key",
        "metadata": {"path": "README.md"},
        "nested": {"authorization": "Bearer secret-key"},
    }
    safe = redact_payload(payload)
    assert "secret-key" not in json.dumps(safe)
    assert safe["content"] == "[tool result redacted]"
    assert "authorization" not in safe["nested"]

    store = LocalStore(tmp_path / "data")
    project = store.ensure_project(tmp_path)
    session = store.create_session(project)
    run = store.create_run(session.id, "inspect", "task", 2)
    store.append_event(session.id, run.id, 0, "tool.completed", payload)
    trace_text = store.trace_path(session.id, run.id).read_text(encoding="utf-8")
    assert "secret-key" not in trace_text
    assert "private source text" not in trace_text


def test_invalid_permission_is_rejected(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    with pytest.raises(InvalidPermissionError):
        store.ensure_project(tmp_path, permission_mode="admin")


def test_data_dir_environment_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEANHARNESS_DATA_DIR", str(tmp_path / "override"))
    assert default_data_dir() == (tmp_path / "override").resolve()


def test_default_title_is_replaced_only_by_first_task(tmp_path: Path) -> None:
    from leanharness.application.session_gateway import apply_first_task_title

    store = LocalStore(tmp_path / "data")
    project = store.ensure_project(tmp_path)
    session = store.create_session(project)
    titled = apply_first_task_title(store, session, "  分析   这个仓库的结构 " + "x" * 80)
    assert titled.title == ("分析 这个仓库的结构 " + "x" * 80)[:40]
    assert apply_first_task_title(store, titled, "second task").title == titled.title

    explicit = store.create_session(project, title="我的会话")
    assert apply_first_task_title(store, explicit, "task").title == "我的会话"


def test_trace_redactor_removes_secret_shapes_and_hidden_reasoning() -> None:
    redactor = TraceRedactor(secrets=("test-secret",))
    payload = {
        "type": "assistant.progress",
        "summary": "<think>private plan</think> use test-secret sk-test_123456789012",
        "analysis": "must not persist",
    }

    safe = redactor.payload(payload)
    text = json.dumps(safe, ensure_ascii=False)
    assert "private plan" not in text
    assert "test-secret" not in text
    assert "sk-test_123456789012" not in text
    assert "analysis" not in safe


def test_messages_and_run_records_are_redacted(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data", redactor=TraceRedactor(secrets=("secret-key",)))
    project = store.ensure_project(tmp_path)
    session = store.create_session(project)
    message = store.add_message(
        session.id, "user", "secret-key <thinking>private plan</thinking> visible"
    )
    run = store.create_run(session.id, "chat", "secret-key task", 1)
    store.update_run(run.id, state="COMPLETED", answer="secret-key answer")

    assert "secret-key" not in message.content
    assert "private plan" not in message.content
    assert "secret-key" not in store.get_session(session.id).title
    assert "secret-key" not in store.list_runs(session.id)[0].task
    assert "secret-key" not in (store.list_runs(session.id)[0].answer or "")


@pytest.mark.parametrize(
    ("task", "expected"),
    [("请分析仓库", "zh"), ("Inspect the repository", "en"), ("Проверь проект", "same")],
)
def test_first_task_locks_session_language(
    tmp_path: Path, task: str, expected: str
) -> None:
    from leanharness.application.session_gateway import apply_first_task_title

    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path))
    updated = apply_first_task_title(store, session, task)
    assert updated.language == expected


def test_v1_database_migrates_without_losing_history(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    database = data / "leanharness.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE projects(id TEXT PRIMARY KEY, root_path TEXT NOT NULL UNIQUE, permission_mode TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE sessions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, title TEXT NOT NULL, permission_mode TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE messages(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, sequence));
            CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, mode TEXT NOT NULL, task TEXT NOT NULL, state TEXT NOT NULL, max_steps INTEGER NOT NULL, answer TEXT, error_code TEXT, started_at TEXT NOT NULL, finished_at TEXT);
            CREATE TABLE run_events(run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE, sequence INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id, sequence));
            INSERT INTO schema_migrations VALUES(1, '2026-01-01T00:00:00+00:00');
            INSERT INTO projects VALUES('p1', 'ROOT', 'inspect', 't', 't');
            INSERT INTO sessions VALUES('s1', 'p1', 'Legacy', 'inspect', 't', 't');
            """.replace("ROOT", str(tmp_path).replace("'", "''"))
        )

    with LocalStore(data) as store:
        legacy = store.get_session("s1")
        assert legacy.title == "Legacy"
        assert legacy.language is None
        versions = store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in versions] == [1, 2]


def test_continuation_capsule_uses_only_previous_public_run_summary(tmp_path: Path) -> None:
    from leanharness.application.session_gateway import continuation_for_session

    store = LocalStore(tmp_path / "data")
    session = store.create_session(
        store.ensure_project(tmp_path), permission_mode="unrestricted"
    )
    run = store.create_run(
        session.id,
        "coding",
        "Create example.py",
        4,
        permission_mode="approve",
    )
    store.append_event(
        session.id,
        run.id,
        0,
        "run.incomplete",
        {
            "type": "run.incomplete",
            "sequence": 0,
            "metadata": {
                "incomplete_reason": "PATCH_INVALID",
                "evidence": {"changed_files": ["example.py"]},
            },
        },
    )
    store.update_run(run.id, state="EXHAUSTED")

    capsule = continuation_for_session(store, store.get_session(session.id))

    assert capsule is not None
    assert capsule.previous_task == "Create example.py"
    assert capsule.previous_state == "EXHAUSTED"
    assert capsule.changed_files == ("example.py",)
    assert capsule.incomplete_reason == "PATCH_INVALID"
    assert capsule.permission_mode == "unrestricted"
