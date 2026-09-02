from pathlib import Path

import pytest

from leanharness.errors import PlanConflictError, PlanFormatError, PlanStateError
from leanharness.planning import PlanState, PlanStepState, parse_plan_markdown
from leanharness.storage import LocalStore


def test_parser_accepts_limited_markdown() -> None:
    title, steps = parse_plan_markdown(
        "# 登录修复\n\n1. **定位问题** - 检查失败测试\n2. **实现修复** - 修改实现并保留兼容性"
    )

    assert title == "登录修复"
    assert [step.title for step in steps] == ["定位问题", "实现修复"]
    assert steps[0].instruction == "检查失败测试"
    assert all(step.state is PlanStepState.PENDING for step in steps)


@pytest.mark.parametrize(
    "markdown",
    [
        "",
        "# only a title",
        "- unordered step",
        "1. first\n3. skipped number",
        "1. first\n  2. nested",
        "```\n1. hidden\n```",
        "| step | detail |\n| --- | --- |",
    ],
)
def test_parser_rejects_ambiguous_markdown(markdown: str) -> None:
    with pytest.raises(PlanFormatError):
        parse_plan_markdown(markdown)


def test_parser_enforces_step_size_and_count() -> None:
    with pytest.raises(PlanFormatError):
        parse_plan_markdown("1. " + "x" * 2_001)
    with pytest.raises(PlanFormatError):
        parse_plan_markdown("\n".join(f"{index}. step" for index in range(1, 34)))


def test_parser_allows_technical_angle_brackets_but_rejects_html() -> None:
    title, steps = parse_plan_markdown(
        '# Demo\n1. **Runtime** - Require Python >=3.12 and keep list[T] types'
    )
    assert title == "Demo"
    assert ">=3.12" in steps[0].instruction
    with pytest.raises(PlanFormatError):
        parse_plan_markdown("# Demo\n1. **Markup** - reject <script>alert(1)</script>")


def test_plan_storage_crud_version_and_step_state(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path))
    title, steps = parse_plan_markdown("# Demo\n1. inspect - inspect the project")
    plan = store.create_plan(
        session.id,
        title=title,
        task="Inspect the project",
        source_markdown="# Demo\n1. inspect - inspect the project",
        steps=steps,
    )

    edited = store.update_plan(
        plan.id,
        version=1,
        title="Edited",
        source_markdown="# Edited\n1. inspect - inspect the project",
        steps=steps,
    )
    assert edited.version == 2
    assert edited.title == "Edited"
    with pytest.raises(PlanConflictError):
        store.update_plan(
            plan.id,
            version=1,
            title="stale",
            source_markdown="# stale\n1. step",
            steps=steps,
        )

    step = edited.steps[0]
    store.update_plan_step(step.id, PlanStepState.RUNNING)
    completed = store.update_plan_step(
        step.id,
        PlanStepState.COMPLETED,
        evidence={"observations": 1},
    )
    assert completed.state is PlanStepState.COMPLETED
    assert completed.evidence == {"observations": 1}
    running = store.update_plan_state(plan.id, PlanState.RUNNING)
    assert running.state is PlanState.RUNNING
    with pytest.raises(PlanStateError):
        store.update_plan(
            plan.id,
            version=2,
            title="no longer editable",
            source_markdown="# invalid\n1. step",
            steps=steps,
        )


def test_plan_migration_includes_attachment_schema(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    versions = store.connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5, 6]


def test_plan_message_is_persisted_and_injected_into_chat_history(tmp_path: Path) -> None:
    from leanharness.application.session_gateway import history_for_session

    store = LocalStore(tmp_path / "data")
    session = store.create_session(store.ensure_project(tmp_path))
    title, steps = parse_plan_markdown("# Demo\n1. inspect - inspect the project")
    plan = store.create_plan(
        session.id,
        title=title,
        task="Inspect the project",
        source_markdown="# Demo\n1. inspect - inspect the project",
        steps=steps,
    )
    store.add_message(session.id, "user", "Inspect the project")
    store.add_message(
        session.id,
        "assistant",
        plan.source_markdown,
        kind="plan",
        plan_id=plan.id,
    )
    store.add_message(session.id, "assistant", "The project is ready.")

    messages = store.list_messages(session.id)

    assert messages[1].kind == "plan"
    assert messages[1].plan_id == plan.id
    assert [message.content for message in history_for_session(store, session)] == [
        "Inspect the project",
        "# Demo\n1. inspect - inspect the project",
        "The project is ready.",
    ]
