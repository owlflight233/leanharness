from __future__ import annotations

import pytest

from leanharness.application.run_intent import is_continuation_message, resolve_run_intent
from leanharness.storage import LocalStore


@pytest.mark.parametrize(
    "message",
    [
        "继续",
        "你继续执行这个任务",
        "再试试",
        "现在呢\uFF1F",
        "还是不行\uFF1F",
        "我换了个权限\uFF0C再试试",
        "我再换了个权限\uFF0C试试",
        "continue",
        "retry",
        "try again",
        "resume",
    ],
)
def test_only_explicit_short_followups_are_continuations(message: str) -> None:
    assert is_continuation_message(message)


@pytest.mark.parametrize("message", ["继续创建 app.py", "please fix the login bug", "继续分析仓库"])
def test_concrete_goal_is_not_a_continuation(message: str) -> None:
    assert not is_continuation_message(message)


def test_continuation_reuses_latest_substantive_task_and_current_permission(tmp_path) -> None:
    store = LocalStore(tmp_path / "data")
    project = store.ensure_project(tmp_path)
    session = store.create_session(project, permission_mode="unrestricted")
    source = store.create_run(
        session.id,
        "coding",
        "Create mini-todo and run the tests",
        24,
        permission_mode="inspect",
    )
    store.update_run(source.id, state="EXHAUSTED", error_code="PERMISSION_INSUFFICIENT")
    followup = store.create_run(session.id, "coding", "继续", 24, permission_mode="unrestricted")
    store.update_run(followup.id, state="FAILED", error_code="MUTATION_NOT_REQUESTED")

    intent = resolve_run_intent(store, session, "我换了个权限\uFF0C再试试")

    assert intent.continued is True
    assert intent.source_run_id == source.id
    assert intent.effective_task == source.task
    assert intent.original_message == "我换了个权限\uFF0C再试试"
    assert intent.requirements.mutation_required is True
    assert intent.requirements.verification_required is True
    assert intent.session_permission_mode == "unrestricted"
    assert intent.continuation is not None
    assert intent.continuation.permission_mode == "unrestricted"
    assert intent.continuation.previous_run_permission_mode == "inspect"


def test_continuation_without_history_remains_a_new_task(tmp_path) -> None:
    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path))

    intent = resolve_run_intent(store, session, "继续")

    assert intent.continued is False
    assert intent.source_run_id is None
    assert intent.effective_task == "继续"


def test_continuation_never_crosses_project_sessions(tmp_path) -> None:
    store = LocalStore(tmp_path / "data")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = store.create_session(store.ensure_project(first_root))
    second = store.create_session(store.ensure_project(second_root))
    source = store.create_run(first.id, "coding", "Create app.py", 4)
    store.update_run(source.id, state="FAILED", error_code="PERMISSION_INSUFFICIENT")

    intent = resolve_run_intent(store, second, "continue")

    assert intent.continued is False
    assert intent.source_run_id is None
