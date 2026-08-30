"""Stable, provider-neutral contracts for real coding-agent evaluations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

ExpectedTerminal = Literal["run.completed", "run.incomplete", "run.cancelled"]
ApprovalPolicy = Literal["none", "approve", "reject"]


@dataclass(frozen=True, slots=True)
class FileExpectation:
    path: str
    contains: tuple[str, ...] = ()
    exact: str | None = None
    absent: bool = False
    unchanged: bool = False


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    id: str
    task: str
    permission_mode: str
    setup_files: dict[str, str] = field(default_factory=dict)
    expected_files: tuple[FileExpectation, ...] = ()
    expected_terminal: ExpectedTerminal = "run.completed"
    approval_policy: ApprovalPolicy = "none"
    require_observation: bool = False
    require_mutation: bool = False
    require_verification: bool = False
    require_user_input: bool = False
    user_input_answers: tuple[str, ...] = ()
    cancel_before_start: bool = False
    max_steps: int = 12
    description: str = ""


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    scenario_id: str
    repetition: int
    passed: bool
    false_completion: bool
    terminal: str
    duration_ms: int
    model_calls: int
    tool_calls: int
    tool_failures: int
    approvals: int
    user_inputs: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    changed_files: tuple[str, ...]
    error_codes: tuple[str, ...]
    failed_checks: tuple[str, ...]
    answer_chars: int
    answer_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    started_at: str
    finished_at: str
    model: str
    results: tuple[EvaluationResult, ...]

    def to_dict(self) -> dict[str, object]:
        total = len(self.results)
        passed = sum(result.passed for result in self.results)
        false_completions = sum(result.false_completion for result in self.results)
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "model": self.model,
            "summary": {
                "runs": total,
                "passed": passed,
                "failed": total - passed,
                "pass_rate": passed / total if total else 0.0,
                "false_completions": false_completions,
                "model_calls": sum(item.model_calls for item in self.results),
                "tool_calls": sum(item.tool_calls for item in self.results),
                "tool_failures": sum(item.tool_failures for item in self.results),
                "user_inputs": sum(item.user_inputs for item in self.results),
                "total_tokens": sum(item.total_tokens for item in self.results),
                "duration_ms": sum(item.duration_ms for item in self.results),
            },
            "results": [result.to_dict() for result in self.results],
        }
