"""Application services for generating and serializing plans."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from leanharness.application.model_gateway import ModelClientFactory
from leanharness.application.model_settings import load_effective_model_config
from leanharness.context import ContextSource
from leanharness.models import ModelConfig, ModelMessage, OpenAICompatibleClient, load_model_config
from leanharness.planning import Plan, PlanStep
from leanharness.planning.generator import PlanGenerator


def create_plan_generator(
    workspace: Path,
    *,
    language: str,
    run_id: str | None = None,
    session_id: str = "ephemeral",
    client_factory: ModelClientFactory = OpenAICompatibleClient,
    history_sources: tuple[ContextSource, ...] = (),
    context_sanitizer: Callable[[str], str] | None = None,
    model_config: ModelConfig | None = None,
    data_dir: str | Path | None = None,
    user_message: ModelMessage | None = None,
) -> PlanGenerator:
    effective_config = model_config
    if effective_config is None:
        effective_config = (
            load_effective_model_config(data_dir)
            if data_dir is not None
            else load_model_config()
        )
    return PlanGenerator(
        workspace,
        client_factory(effective_config),
        language=language,
        run_id=run_id,
        session_id=session_id,
        history_sources=history_sources,
        context_sanitizer=context_sanitizer,
        user_message=user_message,
    )


def plan_to_dict(
    plan: Plan,
    *,
    execution_permission_mode: str | None = None,
) -> dict[str, object]:
    return {
        "id": plan.id,
        "session_id": plan.session_id,
        "title": plan.title,
        "task": plan.task,
        "state": plan.state.value,
        "version": plan.version,
        "source_markdown": plan.source_markdown,
        "run_id": plan.run_id,
        # Pending plans have no execution run yet. Callers may provide the
        # current session preference; attached plans use the immutable run
        # snapshot instead.
        "execution_permission_mode": execution_permission_mode,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "confirmed_at": plan.confirmed_at,
        "finished_at": plan.finished_at,
        "error_code": plan.error_code,
        "steps": [plan_step_to_dict(step) for step in plan.steps],
    }


def plan_step_to_dict(step: PlanStep) -> dict[str, object]:
    return {
        "id": step.id,
        "sequence": step.sequence,
        "title": step.title,
        "instruction": step.instruction,
        "enabled": step.enabled,
        "state": step.state.value,
        "evidence": step.evidence,
        "error_code": step.error_code,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }
