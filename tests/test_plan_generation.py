from __future__ import annotations

import asyncio
from pathlib import Path

from leanharness.models import ModelRequest, ModelResponse, ToolCall
from leanharness.planning.generator import GeneratedPlan, PlanGenerator


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses[len(self.requests) - 1]


def test_plan_generation_inspects_then_parses_markdown(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Demo", encoding="utf-8")
    model = ScriptedModel(
        [
            ModelResponse(
                content="I will inspect the project.",
                tool_calls=(
                    ToolCall(
                        id="read-1",
                        name="workspace_read",
                        arguments={"path": "README.md"},
                    ),
                ),
            ),
            ModelResponse(
                content="# Demo plan\n1. **Inspect** - Read the relevant implementation"
            ),
        ]
    )

    async def collect():
        generator = PlanGenerator(tmp_path, model, language="en")
        return [item async for item in generator.generate("Fix")]

    items = asyncio.run(collect())
    generated = next(item for item in items if isinstance(item, GeneratedPlan))
    assert generated.title == "Demo plan"
    assert generated.steps[0].title == "Inspect"
    assert {tool.name for tool in model.requests[0].tools} == {
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "git_inspect",
    }
    assert all(
        tool.name not in {"workspace_patch", "workspace_command"}
        for request in model.requests
        for tool in request.tools
    )
    assert "Return only a plan" in model.requests[-1].messages[-1].content
