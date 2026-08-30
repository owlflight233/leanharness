"""Execute evaluations in disposable workspaces and emit bounded metrics."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol

from evals.contracts import (
    EvaluationReport,
    EvaluationResult,
    EvaluationScenario,
    FileExpectation,
)
from evals.scenarios import SCENARIOS
from leanharness.application.model_settings import load_effective_model_config
from leanharness.models import (
    ModelRequest,
    ModelResponse,
    OpenAICompatibleClient,
)
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.planning import Plan, PlanController, PlanEvent, PlanState, PlanStep
from leanharness.runtime import CodingAgent, RuntimeEvent
from leanharness.runtime.user_input import UserInputCoordinator


class EvaluationModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


async def evaluate_scenario(
    scenario: EvaluationScenario,
    model_client: EvaluationModelClient,
    *,
    repetition: int = 1,
    temporary_parent: Path | None = None,
) -> EvaluationResult:
    """Run one scenario without retaining its workspace or model-visible source."""

    started = monotonic()
    with tempfile.TemporaryDirectory(
        prefix=f"leanharness-eval-{scenario.id}-",
        dir=temporary_parent,
    ) as temporary_name:
        root = Path(temporary_name)
        workspace = root / "workspace"
        data_dir = root / "data"
        workspace.mkdir()
        data_dir.mkdir()
        _seed_workspace(workspace, scenario.setup_files)
        initial_hashes = _workspace_hashes(workspace)
        cancel_event = asyncio.Event()
        if scenario.cancel_before_start:
            cancel_event.set()
        coordinator = (
            ApprovalCoordinator(timeout_seconds=5)
            if scenario.permission_mode == PermissionMode.APPROVE.value
            else None
        )
        user_inputs = (
            UserInputCoordinator(timeout_seconds=5)
            if scenario.require_user_input or scenario.user_input_answers
            else None
        )
        if scenario.mode == "plan":
            if not scenario.plan_steps:
                raise ValueError("Plan evaluation requires at least one plan step")
            plan = Plan(
                id=f"eval-plan-{scenario.id}",
                session_id=f"eval-session-{scenario.id}",
                title=scenario.id,
                task=scenario.task,
                state=PlanState.RUNNING,
                version=1,
                source_markdown="",
                run_id=f"eval-run-{scenario.id}",
                created_at="evaluation",
                updated_at="evaluation",
                steps=tuple(
                    PlanStep(
                        id=f"eval-step-{index}",
                        sequence=index,
                        title=spec.title,
                        instruction=spec.instruction,
                    )
                    for index, spec in enumerate(scenario.plan_steps, start=1)
                ),
            )
            controller = PlanController(
                plan,
                workspace,
                model_client,
                max_steps=scenario.max_steps,
                cancel_event=cancel_event,
                permission_mode=PermissionMode(scenario.permission_mode),
                approvals=coordinator,
                language="zh",
            )
            stream = controller.run()
            run_id = plan.run_id or ""
        else:
            agent = CodingAgent(
                workspace,
                model_client,
                max_steps=scenario.max_steps,
                cancel_event=cancel_event,
                permission_mode=PermissionMode(scenario.permission_mode),
                approvals=coordinator,
                user_inputs=user_inputs,
                language="zh",
            )
            stream = agent.run(scenario.task)
            run_id = agent.run_id
        events: list[RuntimeEvent | PlanEvent] = []
        pending_answers = iter(scenario.user_input_answers)
        async for event in stream:
            events.append(event)
            if event.type == "approval.required" and coordinator is not None:
                approval_id = str((event.metadata or {})["approval_id"])
                decision = "approve" if scenario.approval_policy == "approve" else "reject"
                coordinator.resolve(run_id, approval_id, decision)
            if event.type == "input.required" and user_inputs is not None:
                try:
                    answer = next(pending_answers)
                except StopIteration:
                    cancel_event.set()
                else:
                    user_inputs.resolve(
                        run_id,
                        str((event.metadata or {})["input_id"]),
                        answer,
                    )
        result = _assess(
            scenario,
            repetition,
            workspace,
            initial_hashes,
            events,
            duration_ms=round((monotonic() - started) * 1000),
        )
    return result


def _assess(
    scenario: EvaluationScenario,
    repetition: int,
    workspace: Path,
    initial_hashes: dict[str, str],
    events: list[RuntimeEvent | PlanEvent],
    *,
    duration_ms: int,
) -> EvaluationResult:
    terminal_event = next(
        (event for event in reversed(events) if event.type.startswith("run.")),
        None,
    )
    terminal = terminal_event.type if terminal_event else "missing"
    failed_checks: list[str] = []
    if terminal != scenario.expected_terminal:
        failed_checks.append(
            f"terminal:{terminal}:expected:{scenario.expected_terminal}"
        )
    for expectation in scenario.expected_files:
        _check_file(expectation, workspace, initial_hashes, failed_checks)

    evidence = (terminal_event.metadata or {}).get("evidence", {}) if terminal_event else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    _check_evidence(scenario, evidence, failed_checks)
    error_codes = tuple(
        str((event.metadata or {}).get("error_code") or event.error_code)
        for event in events
        if (event.metadata or {}).get("error_code") or event.error_code
    )
    tool_failures = sum(
        event.type == "tool.completed" and (event.metadata or {}).get("ok") is False
        for event in events
    )
    approvals = sum(event.type == "approval.required" for event in events)
    user_inputs = sum(event.type == "input.required" for event in events)
    if scenario.require_user_input and user_inputs == 0:
        failed_checks.append("user_input_missing")
    metrics = (terminal_event.metadata or {}).get("metrics", {}) if terminal_event else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    answer = (terminal_event.answer or "") if terminal_event else ""
    false_completion = terminal == "run.completed" and bool(failed_checks)
    return EvaluationResult(
        scenario_id=scenario.id,
        repetition=repetition,
        passed=not failed_checks,
        false_completion=false_completion,
        terminal=terminal,
        duration_ms=duration_ms,
        model_calls=_metric(metrics, "model_calls"),
        tool_calls=_metric(metrics, "tool_calls"),
        tool_failures=tool_failures,
        approvals=approvals,
        user_inputs=user_inputs,
        prompt_tokens=_metric(metrics, "prompt_tokens"),
        completion_tokens=_metric(metrics, "completion_tokens"),
        total_tokens=_metric(metrics, "total_tokens"),
        changed_files=tuple(str(value) for value in evidence.get("changed_files", [])),
        error_codes=error_codes,
        failed_checks=tuple(failed_checks),
        answer_chars=len(answer),
        answer_sha256=(hashlib.sha256(answer.encode("utf-8")).hexdigest() if answer else None),
    )


def _seed_workspace(workspace: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _workspace_hashes(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in workspace.rglob("*")
        if path.is_file()
    }


def _check_file(
    expectation: FileExpectation,
    workspace: Path,
    initial_hashes: dict[str, str],
    failed_checks: list[str],
) -> None:
    path = workspace / expectation.path
    if expectation.absent:
        if path.exists():
            failed_checks.append(f"file_present:{expectation.path}")
        return
    if not path.is_file():
        failed_checks.append(f"file_missing:{expectation.path}")
        return
    raw = path.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        failed_checks.append(f"file_non_utf8:{expectation.path}")
        return
    if expectation.exact is not None and content != expectation.exact:
        failed_checks.append(f"file_exact:{expectation.path}")
    for marker in expectation.contains:
        if marker not in content:
            failed_checks.append(f"file_content:{expectation.path}:{marker}")
    if expectation.unchanged:
        digest = hashlib.sha256(raw).hexdigest()
        if initial_hashes.get(expectation.path) != digest:
            failed_checks.append(f"file_changed:{expectation.path}")


def _check_evidence(
    scenario: EvaluationScenario,
    evidence: dict[str, object],
    failed_checks: list[str],
) -> None:
    requirements = (
        (scenario.require_observation, "observations", "observation_missing"),
        (scenario.require_mutation, "mutations", "mutation_missing"),
        (scenario.require_verification, "verifications", "verification_missing"),
    )
    for required, key, error in requirements:
        value = evidence.get(key, 0)
        if required and (not isinstance(value, int) or value < 1):
            failed_checks.append(error)


def _metric(metrics: dict[str, object], key: str) -> int:
    value = metrics.get(key, 0)
    return value if isinstance(value, int) else 0


async def _run_selected(ids: Sequence[str], repetitions: int) -> EvaluationReport:
    config = load_effective_model_config()
    client = OpenAICompatibleClient(config)
    started = datetime.now(UTC)
    results = []
    for repetition in range(1, repetitions + 1):
        for scenario_id in ids:
            results.append(
                await evaluate_scenario(
                    SCENARIOS[scenario_id],
                    client,
                    repetition=repetition,
                )
            )
    return EvaluationReport(
        started_at=started.isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        model=config.model,
        results=tuple(results),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run isolated LeanHarness evaluations")
    parser.add_argument("--list", action="store_true", help="List scenario IDs and exit")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(SCENARIOS),
        help="Scenario to run; repeat for multiple scenarios",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        for scenario in SCENARIOS.values():
            print(f"{scenario.id}: {scenario.description}")
        return 0
    if not args.scenario:
        raise SystemExit("Select at least one --scenario to avoid accidental model charges")
    if not 1 <= args.repetitions <= 20:
        raise SystemExit("--repetitions must be between 1 and 20")
    report = asyncio.run(_run_selected(args.scenario, args.repetitions))
    serialized = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if all(result.passed for result in report.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
