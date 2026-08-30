"""Application service exposing the coding runtime to interfaces."""

from __future__ import annotations

from pathlib import Path

from leanharness.application.model_gateway import ModelClientFactory
from leanharness.errors import RunInputError
from leanharness.models import ModelMessage, OpenAICompatibleClient, load_model_config
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.runtime import (
    CodingAgent,
    RunControlError,
    validate_run_task,
)
from leanharness.runtime.loop import MAX_MAX_STEPS, MIN_MAX_STEPS


def create_coding_run(
    task: str,
    workspace: Path,
    *,
    max_steps: int = 24,
    client_factory: ModelClientFactory = OpenAICompatibleClient,
    run_id: str | None = None,
    language: str = "same",
    permission_mode: str = "inspect",
    session_id: str = "ephemeral",
    approvals: ApprovalCoordinator | None = None,
    history: tuple[ModelMessage, ...] = (),
) -> CodingAgent:
    """Validate public input and create a bounded coding runtime."""

    try:
        validate_run_task(task)
    except RunControlError as exc:
        raise RunInputError(exc.message) from exc
    if not MIN_MAX_STEPS <= max_steps <= MAX_MAX_STEPS:
        raise RunInputError(f"max_steps must be between {MIN_MAX_STEPS} and {MAX_MAX_STEPS}")
    model = client_factory(load_model_config())
    try:
        mode = PermissionMode(permission_mode)
    except ValueError as exc:
        raise RunInputError("permission_mode is invalid") from exc
    return CodingAgent(
        workspace,
        model,
        max_steps=max_steps,
        run_id=run_id,
        language=language,
        permission_mode=mode,
        session_id=session_id,
        approvals=approvals,
        history=history,
    )


# Compatibility for clients created before guarded coding tools were introduced.
create_inspection_run = create_coding_run
