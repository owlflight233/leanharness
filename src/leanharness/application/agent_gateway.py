"""Application service exposing the coding runtime to interfaces."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from leanharness.application.model_gateway import ModelClientFactory
from leanharness.application.model_settings import load_effective_model_config
from leanharness.context import ContextSource
from leanharness.errors import RunInputError
from leanharness.models import ModelConfig, ModelMessage, OpenAICompatibleClient, load_model_config
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.runtime import (
    CodingAgent,
    RunControlError,
    UserInputCoordinator,
    validate_run_task,
)
from leanharness.runtime.loop import MAX_MAX_STEPS, MIN_MAX_STEPS
from leanharness.tools import ToolRegistry


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
    user_inputs: UserInputCoordinator | None = None,
    history: tuple[ModelMessage, ...] = (),
    history_sources: tuple[ContextSource, ...] = (),
    context_sanitizer: Callable[[str], str] | None = None,
    model_config: ModelConfig | None = None,
    data_dir: str | Path | None = None,
    user_message: ModelMessage | None = None,
    tool_registry_factory: Callable[..., ToolRegistry] = ToolRegistry,
) -> CodingAgent:
    """Validate public input and create a bounded coding runtime."""

    try:
        validate_run_task(task)
    except RunControlError as exc:
        raise RunInputError(exc.message) from exc
    if not MIN_MAX_STEPS <= max_steps <= MAX_MAX_STEPS:
        raise RunInputError(f"max_steps must be between {MIN_MAX_STEPS} and {MAX_MAX_STEPS}")
    effective_config = model_config
    if effective_config is None:
        effective_config = (
            load_effective_model_config(data_dir)
            if data_dir is not None
            else load_model_config()
        )
    model = client_factory(effective_config)
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
        user_inputs=user_inputs,
        history=history,
        history_sources=history_sources,
        context_sanitizer=context_sanitizer,
        user_message=user_message,
        tool_registry_factory=tool_registry_factory,
    )


# Compatibility for clients created before guarded coding tools were introduced.
create_inspection_run = create_coding_run
