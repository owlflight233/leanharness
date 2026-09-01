from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from leanharness.errors import InvalidPermissionError, SessionNotFoundError, StorageError
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


def test_projects_keep_creation_order_and_reads_do_not_touch_metadata(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    roots = [tmp_path / name for name in ("a", "b", "c")]
    for root in roots:
        root.mkdir()
    created = [store.ensure_project(root) for root in roots]

    for index in (0, 2, 1, 0):
        observed = store.ensure_project(roots[index], permission_mode="unrestricted")
        assert observed.updated_at == created[index].updated_at
        assert observed.permission_mode == created[index].permission_mode

    assert [project.root_path for project in store.list_projects()] == [
        str(root.resolve()) for root in roots
    ]


def test_history_for_session_returns_public_conversation_messages_only(tmp_path: Path) -> None:
    from leanharness.application.session_gateway import history_for_session

    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path))
    store.add_message(session.id, "user", "第一项任务")
    store.add_message(session.id, "assistant", "已检查项目")
    store.add_message(session.id, "progress", "内部行动")
    history = history_for_session(store, session)
    assert [(message.role, message.content) for message in history] == [
        ("user", "第一项任务"),
        ("assistant", "已检查项目"),
    ]


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
        assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6]


def test_attachment_lifecycle_binds_to_message_and_deletes_with_session(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    with LocalStore(data) as store:
        project = store.ensure_project(tmp_path)
        session = store.create_session(project)
        attachment = store.create_attachment(
            session.id,
            "../notes.py",
            "text/plain",
            b"print('ready')\n",
        )
        assert attachment.filename == "notes.py"
        assert attachment.kind == "text"
        attachment_path = data / attachment.storage_path
        assert attachment_path.is_file()
        message = store.add_message(
            session.id,
            "user",
            "Review this file",
            attachment_ids=(attachment.id,),
        )
        bound = store.get_attachment(attachment.id)
        assert bound.message_id == message.id
        assert store.read_attachment(attachment.id, session_id=session.id) == b"print('ready')\n"
        store.delete_session(session.id)
        assert not attachment_path.exists()


def test_attachment_validates_image_bytes_and_session_ownership(tmp_path: Path) -> None:
    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(image_bytes, format="PNG")
    with LocalStore(tmp_path / "data") as store:
        project = store.ensure_project(tmp_path)
        first = store.create_session(project)
        second = store.create_session(project)
        attachment = store.create_attachment(
            first.id,
            "screen.png",
            "image/png",
            image_bytes.getvalue(),
        )
        assert attachment.kind == "image"
        assert attachment.media_type == "image/png"
        with pytest.raises(StorageError, match="does not belong"):
            store.read_attachment(attachment.id, session_id=second.id)
        with pytest.raises(StorageError, match="does not match"):
            store.create_attachment(first.id, "fake.png", "image/jpeg", image_bytes.getvalue())
        with pytest.raises(StorageError, match="not a valid"):
            store.create_attachment(first.id, "broken.png", "image/png", b"not an image")


def test_attachment_legacy_absolute_path_is_repaired_when_data_dir_is_relocated(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    with LocalStore(data) as store:
        project = store.ensure_project(tmp_path)
        session = store.create_session(project)
        attachment = store.create_attachment(session.id, "notes.txt", "text/plain", b"ready")
        legacy_path = (
            "C:\\Users\\Dell\\AppData\\Local\\Packages\\Codex\\LocalCache\\Local\\"
            "LeanHarness\\attachments\\"
            f"{attachment.id}.bin"
        )
        store.connection.execute(
            "UPDATE attachments SET storage_path=? WHERE id=?",
            (legacy_path, attachment.id),
        )
        store.connection.commit()

    with LocalStore(data) as reopened:
        repaired = reopened.get_attachment(attachment.id)
        assert repaired.storage_path == f"attachments/{attachment.id}.bin"
        assert reopened.read_attachment(attachment.id, session_id=session.id) == b"ready"


def test_attachment_rejects_unsupported_text_and_mime_or_duplicate_binding(tmp_path: Path) -> None:
    with LocalStore(tmp_path / "data") as store:
        session = store.create_session(store.ensure_project(tmp_path))
        with pytest.raises(StorageError, match="not supported"):
            store.create_attachment(session.id, "archive.zip", "application/zip", b"PK")
        with pytest.raises(StorageError, match="UTF-8"):
            store.create_attachment(session.id, "bad.txt", "text/plain", b"\xff")
        attachment = store.create_attachment(session.id, "a.txt", "text/plain", b"ok")
        store.add_message(session.id, "user", "first", attachment_ids=(attachment.id,))
        with pytest.raises(StorageError, match="already bound"):
            store.add_message(session.id, "user", "second", attachment_ids=(attachment.id,))


def test_session_detail_includes_only_attachment_metadata(tmp_path: Path) -> None:
    from leanharness.application.session_gateway import session_detail

    with LocalStore(tmp_path / "data") as store:
        session = store.create_session(store.ensure_project(tmp_path))
        attachment = store.create_attachment(session.id, "notes.txt", "text/plain", b"secret source")
        message = store.add_message(
            session.id, "user", "Review", attachment_ids=(attachment.id,)
        )
        payload = session_detail(store, session.id)
        message_payload = next(item for item in payload["messages"] if item["id"] == message.id)
        assert message_payload["attachments"][0]["filename"] == "notes.txt"
        assert "secret source" not in json.dumps(payload, ensure_ascii=False)


def test_model_attachment_text_is_bounded_without_mutating_stored_file(tmp_path: Path) -> None:
    from leanharness.application.attachments import (
        MAX_MODEL_ATTACHMENT_TEXT_CHARS,
        message_with_attachments,
    )

    content = "x" * (MAX_MODEL_ATTACHMENT_TEXT_CHARS + 5_000)
    with LocalStore(tmp_path / "data") as store:
        session = store.create_session(store.ensure_project(tmp_path))
        attachment = store.create_attachment(session.id, "large.txt", "text/plain", content.encode())
        message = message_with_attachments(store, session.id, "Review", (attachment.id,))
        assert len(message.content) < MAX_MODEL_ATTACHMENT_TEXT_CHARS + 500
        assert "content truncated" in message.content
        assert len(store.read_attachment(attachment.id, session_id=session.id)) == len(content)
