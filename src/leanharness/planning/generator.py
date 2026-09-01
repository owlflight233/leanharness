"""Read-only plan generation built on the fixed coding runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from leanharness.application.language import language_instruction
from leanharness.context import ContextSource
from leanharness.models import ModelMessage, ModelRequest, ModelResponse
from leanharness.permissions import PermissionMode
from leanharness.planning.contracts import PlanStep
from leanharness.planning.parser import parse_plan_markdown
from leanharness.runtime import CodingAgent, RuntimeEvent

PLAN_GENERATION_MAX_STEPS = 8


def plan_generation_task(task: str) -> str:
    return (
        "Inspect the repository and any supplied attachments as needed, then produce an "
        "implementation plan for this request. Do not modify files or run commands. "
        f"Request: {task}"
    )


@dataclass(frozen=True, slots=True)
class GeneratedPlan:
    title: str
    markdown: str
    steps: tuple[PlanStep, ...]


class PlanningModel(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class PlanningModelClient:
    """Add the limited Markdown contract to the final answer request."""

    def __init__(self, delegate: PlanningModel, language: str) -> None:
        self._delegate = delegate
        self._language = language

    async def complete(self, request: ModelRequest) -> ModelResponse:
        messages = request.messages
        from leanharness.models import ModelMessage

        messages = (
            *messages,
            ModelMessage(
                role="user",
                content=(
                    "When you are ready to finish, Return only a plan in this exact limited "
                    "Markdown shape: an optional single '# Title' line followed by a "
                    "consecutive ordered list. Each step must be 'N. **Short title** - "
                    "concrete instruction'. Do not use code fences, tables, nested lists, "
                    "bullets, preamble, or epilogue. Use tools first when evidence is needed. "
                    + language_instruction(self._language)
                ),
            ),
        )
        return await self._delegate.complete(
            ModelRequest(
                messages=messages,
                max_tokens=request.max_tokens,
                stream=request.stream,
                tools=request.tools,
                tool_choice=request.tool_choice,
            )
        )


class PlanGenerator:
    def __init__(
        self,
        workspace: Path,
        model_client: PlanningModel,
        *,
        language: str,
        run_id: str | None = None,
        session_id: str = "ephemeral",
        history_sources: tuple[ContextSource, ...] = (),
        context_sanitizer: Callable[[str], str] | None = None,
        user_message: ModelMessage | None = None,
    ) -> None:
        self.agent = CodingAgent(
            workspace,
            PlanningModelClient(model_client, language),
            max_steps=PLAN_GENERATION_MAX_STEPS,
            language=language,
            permission_mode=PermissionMode.INSPECT,
            run_id=run_id,
            session_id=session_id,
            history_sources=history_sources,
            context_sanitizer=context_sanitizer,
            include_outcome_tool=False,
            user_message=user_message,
        )

    async def generate(self, task: str) -> AsyncIterator[RuntimeEvent | GeneratedPlan]:
        planning_task = plan_generation_task(task)
        async for event in self.agent.run(planning_task):
            if event.type == "run.completed" and event.answer:
                title, steps = parse_plan_markdown(event.answer)
                yield GeneratedPlan(title=title, markdown=event.answer, steps=steps)
            yield event
