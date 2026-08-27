from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanharness.errors import InvalidPermissionError, SessionNotFoundError
from leanharness.storage import LocalStore, default_data_dir, redact_payload


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
