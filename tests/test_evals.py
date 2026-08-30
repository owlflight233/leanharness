from __future__ import annotations

import asyncio
import json
from pathlib import Path

from evals.contracts import (
    EvaluationReport,
    EvaluationScenario,
    FileExpectation,
    PlanStepSpec,
)
from evals.runner import evaluate_scenario, main

from leanharness.models import ModelRequest, ModelResponse, ToolCall
from leanharness.runtime.outcome import OUTCOME_TOOL_NAME


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses[len(self.requests) - 1]


def tool_response(call_id: str, name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
    )


def outcome(status: str, answer: str) -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(
            ToolCall(
                id=f"outcome-{status}",
                name=OUTCOME_TOOL_NAME,
                arguments={"status": status, "answer": answer},
            ),
        ),
    )


def input_response() -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(
            ToolCall(
                id="input-1",
                name="request_user_input",
                arguments={
                    "question": "Which filename should I create?",
                    "options": [
                        {"label": "notes.txt", "description": "Create notes.txt."},
                        {"label": "status.txt", "description": "Create status.txt."},
                    ],
                },
            ),
        ),
    )


def run_eval(
    scenario: EvaluationScenario,
    model: ScriptedModel,
    temporary_parent: Path,
):
    return asyncio.run(
        evaluate_scenario(
            scenario,
            model,
            temporary_parent=temporary_parent,
        )
    )


def test_evaluation_runs_in_disposable_workspace_and_records_metrics(tmp_path: Path) -> None:
    scenario = EvaluationScenario(
        id="create",
        task="Create result.txt",
        permission_mode="unrestricted",
        expected_files=(FileExpectation("result.txt", exact="ready\n"),),
        require_mutation=True,
    )
    model = ScriptedModel(
        [
            tool_response(
                "write-1",
                "workspace_write",
                {"path": "result.txt", "content": "ready\n", "mode": "create"},
            ),
            outcome("completed", "Created result.txt."),
        ]
    )

    result = run_eval(scenario, model, tmp_path)

    assert result.passed is True
    assert result.false_completion is False
    assert result.model_calls == 2
    assert result.tool_calls == 1
    assert result.changed_files == ("result.txt",)
    assert result.answer_chars == len("Created result.txt.")
    assert result.answer_sha256 is not None
    assert list(tmp_path.iterdir()) == []


def test_evaluation_detects_false_completion_without_mutation(tmp_path: Path) -> None:
    scenario = EvaluationScenario(
        id="false-completion",
        task="Create missing.txt",
        permission_mode="unrestricted",
        expected_files=(FileExpectation("missing.txt"),),
        require_mutation=True,
    )
    model = ScriptedModel([ModelResponse(content="Done.")])

    result = run_eval(scenario, model, tmp_path)

    assert result.passed is False
    assert result.false_completion is True
    assert "file_missing:missing.txt" in result.failed_checks
    assert "mutation_missing" in result.failed_checks


def test_rejected_approval_preserves_file_and_accepts_incomplete(tmp_path: Path) -> None:
    scenario = EvaluationScenario(
        id="reject",
        task="Change protected.txt",
        permission_mode="approve",
        setup_files={"protected.txt": "before\n"},
        expected_files=(FileExpectation("protected.txt", unchanged=True),),
        expected_terminal="run.incomplete",
        approval_policy="reject",
    )
    model = ScriptedModel(
        [
            tool_response(
                "write-1",
                "workspace_write",
                {
                    "path": "protected.txt",
                    "content": "after\n",
                    "mode": "replace",
                    "expected_sha256": "0" * 64,
                },
            ),
            outcome("incomplete", "The user rejected the requested change."),
        ]
    )

    result = run_eval(scenario, model, tmp_path)

    assert result.passed is True
    assert result.approvals == 1
    assert "APPROVAL_REJECTED" in result.error_codes


def test_pre_cancelled_evaluation_never_calls_model(tmp_path: Path) -> None:
    scenario = EvaluationScenario(
        id="cancel",
        task="Inspect",
        permission_mode="inspect",
        expected_terminal="run.cancelled",
        cancel_before_start=True,
    )
    model = ScriptedModel([])

    result = run_eval(scenario, model, tmp_path)

    assert result.passed is True
    assert result.model_calls == 0
    assert model.requests == []


def test_evaluation_resolves_required_model_input_and_records_it(tmp_path: Path) -> None:
    scenario = EvaluationScenario(
        id="clarify",
        task="Ask for a filename, then create it.",
        permission_mode="unrestricted",
        expected_files=(FileExpectation("notes.txt", exact="ready\n"),),
        require_mutation=True,
        require_user_input=True,
        user_input_answers=("notes.txt",),
    )
    model = ScriptedModel(
        [
            input_response(),
            tool_response(
                "write-1",
                "workspace_write",
                {"path": "notes.txt", "content": "ready\n", "mode": "create"},
            ),
            outcome("completed", "Created notes.txt after clarification."),
        ]
    )

    result = run_eval(scenario, model, tmp_path)

    assert result.passed is True
    assert result.user_inputs == 1
    assert '"answer":"notes.txt"' in next(
        message.content
        for message in model.requests[1].messages
        if message.role == "tool" and message.tool_call_id == "input-1"
    )


def test_plan_evaluation_aggregates_mutation_and_verification_evidence(
    tmp_path: Path,
) -> None:
    scenario = EvaluationScenario(
        id="plan",
        task="Create and verify a module.",
        permission_mode="unrestricted",
        mode="plan",
        plan_steps=(
            PlanStepSpec("Create", "Create arithmetic.py and its test."),
            PlanStepSpec("Verify", "Run pytest."),
        ),
        expected_files=(
            FileExpectation("arithmetic.py", contains=("def add",)),
            FileExpectation("test_arithmetic.py", contains=("test_add",)),
        ),
        require_mutation=True,
        require_verification=True,
        max_steps=8,
    )
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        "write-code",
                        "workspace_write",
                        {
                            "path": "arithmetic.py",
                            "content": "def add(a, b):\n    return a + b\n",
                            "mode": "create",
                        },
                    ),
                    ToolCall(
                        "write-test",
                        "workspace_write",
                        {
                            "path": "test_arithmetic.py",
                            "content": (
                                "from arithmetic import add\n\n\n"
                                "def test_add():\n    assert add(2, 3) == 5\n"
                            ),
                            "mode": "create",
                        },
                    ),
                ),
            ),
            outcome("completed", "Created the module and test."),
            tool_response(
                "verify",
                "workspace_command",
                {"profile": "python-test", "args": ["test_arithmetic.py", "-q"]},
            ),
            outcome("completed", "Pytest passed."),
        ]
    )

    result = run_eval(scenario, model, tmp_path)

    assert result.passed is True
    assert result.terminal == "run.completed"
    assert result.changed_files == ("arithmetic.py", "test_arithmetic.py")
    assert result.model_calls == 4


def test_report_aggregates_without_answer_or_source_text() -> None:
    result = asyncio.run(
        evaluate_scenario(
            EvaluationScenario(
                id="answer-redaction",
                task="Answer",
                permission_mode="inspect",
            ),
            ScriptedModel([ModelResponse(content="private final answer")]),
        )
    )
    report = EvaluationReport(
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        model="example",
        results=(result,),
    )

    serialized = json.dumps(report.to_dict())

    assert report.to_dict()["summary"]["runs"] == 1  # type: ignore[index]
    assert "private final answer" not in serialized


def test_eval_cli_lists_scenarios_without_model_configuration(capsys) -> None:
    assert main(["--list"]) == 0
    assert "create_tested_project" in capsys.readouterr().out
