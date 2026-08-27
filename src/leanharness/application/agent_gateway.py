"""Application service exposing the read-only runtime to interfaces."""

from __future__ import annotations

from pathlib import Path

from leanharness.application.model_gateway import ModelClientFactory
from leanharness.errors import RunInputError
from leanharness.models import OpenAICompatibleClient, load_model_config
from leanharness.runtime import ReadOnlyAgent, RunControlError, validate_run_task
from leanharness.runtime.loop import MAX_MAX_STEPS, MIN_MAX_STEPS


def create_inspection_run(
    task: str,
    workspace: Path,
    *,
    max_steps: int = 24,
    client_factory: ModelClientFactory = OpenAICompatibleClient,
    run_id: str | None = None,
    language: str = "same",
) -> ReadOnlyAgent:
    """Validate public input and create an ephemeral inspection runtime."""

    try:
        validate_run_task(task)
    except RunControlError as exc:
        raise RunInputError(exc.message) from exc
    if not MIN_MAX_STEPS <= max_steps <= MAX_MAX_STEPS:
        raise RunInputError(f"max_steps must be between {MIN_MAX_STEPS} and {MAX_MAX_STEPS}")
    model = client_factory(load_model_config())
    return ReadOnlyAgent(
        workspace, model, max_steps=max_steps, run_id=run_id, language=language
    )
