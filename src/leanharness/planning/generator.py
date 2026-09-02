"""Read-only plan generation built on the fixed coding runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from leanharness.context import ContextSource
from leanharness.errors import PlanFormatError
from leanharness.models import ModelMessage, ModelRequest, ModelResponse
from leanharness.permissions import PermissionMode
from leanharness.planning.contracts import PlanStep
from leanharness.planning.parser import parse_plan_markdown
from leanharness.runtime import CodingAgent, RuntimeEvent
from leanharness.runtime.prompting import language_instruction

PLAN_GENERATION_MAX_STEPS = 8
PLAN_FORMAT_REPAIR_MAX_TOKENS = 4_096


def plan_generation_task(task: str) -> str:
    return (
        "Inspect the repository and any supplied attachments as needed, then produce an "
        "implementation plan for this request. Do not modify files or run commands. "
        f"Request: {task}"
    )


def plan_system_prompt(language: str) -> str:
    """System contract for planning, separate from the coding-loop contract."""

    return (
        "You are LeanHarness Plan Mode. Investigate the supplied repository and attachments "
        "with read-only tools when evidence is needed. Do not edit files, run commands, use "
        "plugins, delegate work, or call a completion tool. The only task-level decision is "
        "to produce a practical implementation plan for the user's request. When the plan "
        "is ready, respond directly with only an optional '# Title' line followed by a "
        "consecutive ordered list. Each step must be 'N. **Short title** - concrete "
        "instruction'. Do not include a preamble, epilogue, code fence, table, unordered "
        "list, or hidden reasoning. "
        + language_instruction(language)
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
        self._model_client = model_client
        self._language = language
        self._task = ""
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
            system_message=plan_system_prompt(language),
        )

    async def generate(self, task: str) -> AsyncIterator[RuntimeEvent | GeneratedPlan]:
        self._task = task
        planning_task = plan_generation_task(task)
        async for event in self.agent.run(planning_task):
            # Forward the runtime terminal event before parsing its answer.
            # A malformed plan is a normal protocol outcome handled by the
            # API boundary; it must not strand the run in CREATED by escaping
            # before the event is observed.
            yield event
            if event.type == "run.completed" and event.answer:
                try:
                    title, steps = parse_plan_markdown(event.answer)
                except PlanFormatError:
                    repaired = await self._repair_format(event.answer)
                    if repaired is not None:
                        yield repaired
                    continue
                yield GeneratedPlan(title=title, markdown=event.answer, steps=steps)

    async def _repair_format(self, candidate: str) -> GeneratedPlan | None:
        """Ask the model once to normalize formatting without changing scope."""

        request = ModelRequest(
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "Normalize a proposed implementation plan into the required limited "
                        "Markdown protocol. Preserve the original task and step meaning. "
                        "Return only an optional '# Title' followed by a consecutive ordered "
                        "list. Each line must be 'N. **Short title** - concrete instruction'. "
                        "Do not add, remove, or reorder work. Do not use tools, code fences, "
                        "tables, bullets, preamble, or epilogue. "
                        + language_instruction(self._language)
                    ),
                ),
                ModelMessage(
                    role="user",
                    content=(
                        f"Original request:\n{self._task}\n\n"
                        "Candidate plan to normalize:\n"
                        f"{candidate[:32 * 1024]}"
                    ),
                ),
            ),
            max_tokens=PLAN_FORMAT_REPAIR_MAX_TOKENS,
            tools=(),
            tool_choice="none",
        )
        try:
            response = await self._model_client.complete(request)
            if response.tool_calls or not response.content:
                return None
            title, steps = parse_plan_markdown(response.content)
        except (PlanFormatError, ValueError, TypeError):
            return None
        return GeneratedPlan(title=title, markdown=response.content, steps=steps)
