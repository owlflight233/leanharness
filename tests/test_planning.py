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


def test_plan_migration_is_version_three(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "data")
    versions = store.connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [row["version"] for row in versions] == [1, 2, 3]
