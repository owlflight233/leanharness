"""Deterministic evidence requirements for accepting a model completion candidate."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from leanharness.tools import ToolResult

_MUTATION_INTENT = re.compile(
    r"\b(add|build|change|create|delete|edit|fix|implement|modify|remove|rename|update|write)\b"
    r"|(?:修改|改动|更改|创建|新建|新增|添加|实现|修复|删除|移除|重命名|写入)",
    re.IGNORECASE,
)
_VERIFICATION_INTENT = re.compile(
    r"\b(run|execute)\s+(?:the\s+)?(?:tests?|lint|typecheck|build)\b"
    r"|(?:运行|执行)(?:测试|检查|构建|类型检查)",
    re.IGNORECASE,
)
_NEGATED_MUTATION = re.compile(
    r"(?:(?:不要|不必|禁止|勿|无需|不需要|不允许|先别)\s*(?:去)?|"
    r"(?:do\s+not|don't|without|no)\s+)"
    r"(?:修改|改动|更改|创建|新建|新增|添加|实现|修复|删除|移除|重命名|写入|写|"
    r"modify|change|create|delete|edit|fix|implement|remove|rename|update|write)",
    re.IGNORECASE,
)
_NEGATED_VERIFICATION = re.compile(
    r"(?:(?:不要|不必|禁止|勿|无需|不需要|不允许|先别)\s*(?:去)?|"
    r"(?:do\s+not|don't|without|no)\s+)"
    r"(?:运行|执行|测试|检查|构建|run|execute|test|check|build)",
    re.IGNORECASE,
)
_OBSERVATION_TOOLS = frozenset(
    {"workspace_list", "workspace_read", "workspace_search", "git_inspect"}
)
_MUTATION_TOOLS = frozenset({"workspace_mkdir", "workspace_patch"})


@dataclass(frozen=True, slots=True)
class TaskRequirements:
    mutation_required: bool
    verification_required: bool

    @classmethod
    def infer(cls, task: str) -> TaskRequirements:
        normalized = _NEGATED_MUTATION.sub("", task)
        verification_text = _NEGATED_VERIFICATION.sub("", task)
        return cls(
            mutation_required=bool(_MUTATION_INTENT.search(normalized)),
            verification_required=bool(_VERIFICATION_INTENT.search(verification_text)),
        )


def missing_capabilities(
    requirements: TaskRequirements, available_tools: set[str] | frozenset[str]
) -> tuple[str, ...]:
    """Return capabilities required by a task but absent from the tool registry."""
    missing: list[str] = []
    if requirements.mutation_required and not (_MUTATION_TOOLS & available_tools):
        missing.append("workspace_mutation")
    if requirements.verification_required and "workspace_command" not in available_tools:
        missing.append("workspace_verification")
    return tuple(missing)


@dataclass(slots=True)
class CompletionLedger:
    """Run-owned public evidence; raw tool content never enters this structure."""

    successful_observations: int = 0
    successful_mutations: int = 0
    successful_verifications: int = 0
    changed_files: set[str] = field(default_factory=set)
    unresolved_errors: list[str] = field(default_factory=list)

    def record(self, tool: str, result: ToolResult) -> None:
        if result.ok:
            if tool in _OBSERVATION_TOOLS:
                self.successful_observations += 1
            elif tool in {"workspace_mkdir", "workspace_patch"}:
                self.successful_mutations += 1
                paths = result.public_metadata.get("files", [])
                if tool == "workspace_mkdir":
                    paths = result.public_metadata.get("created_paths", [])
                if isinstance(paths, list):
                    self.changed_files.update(str(path) for path in paths)
                self._clear_tool_errors("PATCH_")
                self._clear_tool_errors("DIRECTORY_")
            elif tool == "workspace_command":
                self.successful_verifications += 1
                self._clear_tool_errors("COMMAND_")
            return
        if result.error and result.error.code not in self.unresolved_errors:
            self.unresolved_errors.append(result.error.code)

    def completion_decision(self, requirements: TaskRequirements) -> CompletionDecision:
        if requirements.mutation_required and not self.successful_mutations:
            return CompletionDecision(
                accepted=False,
                reason="MUTATION_NOT_APPLIED",
                guidance=(
                    "The task requests a workspace change, but no workspace_mkdir or "
                    "workspace_patch call has succeeded. Continue with an appropriate "
                    "workspace mutation, or state the concrete blocker in the reserved "
                    "summary round."
                ),
            )
        if requirements.verification_required and not self.successful_verifications:
            return CompletionDecision(
                accepted=False,
                reason="VERIFICATION_NOT_RUN",
                guidance=(
                    "The task explicitly requests project verification, but no "
                    "workspace_command call has succeeded. Run an allowed verification "
                    "profile, or state the concrete blocker in the reserved summary round."
                ),
            )
        if not requirements.mutation_required and not self.successful_observations:
            if requirements.verification_required:
                return CompletionDecision(accepted=True)
            return CompletionDecision(
                accepted=False,
                reason="WORKSPACE_NOT_OBSERVED",
                guidance=(
                    "Use a read-only workspace tool to obtain verifiable evidence before "
                    "giving a final answer."
                ),
            )
        return CompletionDecision(accepted=True)

    def public_summary(self) -> dict[str, object]:
        return {
            "observations": self.successful_observations,
            "mutations": self.successful_mutations,
            "verifications": self.successful_verifications,
            "changed_files": sorted(self.changed_files),
            "unresolved_errors": list(self.unresolved_errors),
        }

    def _clear_tool_errors(self, prefix: str) -> None:
        self.unresolved_errors = [
            code for code in self.unresolved_errors if not code.startswith(prefix)
        ]


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    accepted: bool
    reason: str | None = None
    guidance: str | None = None
