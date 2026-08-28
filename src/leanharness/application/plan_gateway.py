"""Application services for generating and serializing plans."""

from __future__ import annotations

from pathlib import Path

from leanharness.application.model_gateway import ModelClientFactory
from leanharness.models import OpenAICompatibleClient, load_model_config
from leanharness.planning import Plan, PlanStep
from leanharness.planning.generator import PlanGenerator


def create_plan_generator(
    workspace: Path,
    *,
    language: str,
    client_factory: ModelClientFactory = OpenAICompatibleClient,
) -> PlanGenerator:
    return PlanGenerator(workspace, client_factory(load_model_config()), language=language)


def plan_to_dict(plan: Plan) -> dict[str, object]:
    return {
        "id": plan.id,
        "session_id": plan.session_id,
        "title": plan.title,
        "task": plan.task,
        "state": plan.state.value,
        "version": plan.version,
        "source_markdown": plan.source_markdown,
        "run_id": plan.run_id,
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
