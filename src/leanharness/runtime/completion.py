"""Run evidence used to validate a model-owned terminal decision."""

from __future__ import annotations

from dataclasses import dataclass, field

from leanharness.tools import ToolResult

_OBSERVATION_TOOLS = frozenset(
    {
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "git_inspect",
        "delegate_analysis",
    }
)
_MUTATION_TOOLS = frozenset(
    {
        "workspace_mkdir",
        "workspace_patch",
        "workspace_write",
        "workspace_edit",
    }
)


@dataclass(slots=True)
class CompletionLedger:
    """Run-owned public evidence; raw tool content never enters this structure."""

    successful_observations: int = 0
    mutation_attempts: int = 0
    successful_mutations: int = 0
    verification_attempts: int = 0
    successful_verifications: int = 0
    changed_files: set[str] = field(default_factory=set)
    unresolved_errors: list[str] = field(default_factory=list)
    unresolved_mutation_errors: list[str] = field(default_factory=list)
    verification_argument_denials: int = 0
    verification_recoveries: int = 0
    verification_failures: list[dict[str, object]] = field(default_factory=list)
    verification_profiles: set[str] = field(default_factory=set)

    @property
    def primary_error_code(self) -> str | None:
        """Return the first unresolved error, preserving the causal failure."""
        return self.unresolved_errors[0] if self.unresolved_errors else None

    def record(self, tool: str, result: ToolResult) -> None:
        plugin_mutation = isinstance(result.public_metadata.get("plugin_id"), str)
        if (
            tool in {"workspace_mkdir", "workspace_patch", "workspace_write", "workspace_edit"}
            or plugin_mutation
        ):
            self.mutation_attempts += 1
        elif tool == "workspace_command":
            self.verification_attempts += 1
        if result.ok:
            if tool in _OBSERVATION_TOOLS:
                self.successful_observations += 1
            elif tool in {
                "workspace_mkdir",
                "workspace_patch",
                "workspace_write",
                "workspace_edit",
            } or plugin_mutation:
                self.successful_mutations += 1
                # Patch returns ``files`` while structured write/edit tools
                # return a single ``path``. Normalize both into one audit
                # field so completion summaries and persisted traces agree.
                paths = result.public_metadata.get("files", [])
                if tool == "workspace_mkdir":
                    paths = result.public_metadata.get("created_paths", [])
                elif tool in {"workspace_write", "workspace_edit"} or plugin_mutation:
                    path = result.public_metadata.get("path")
                    paths = [path] if isinstance(path, str) else []
                if isinstance(paths, list):
                    self.changed_files.update(str(path) for path in paths)
                self._clear_tool_errors("PATCH_")
                self._clear_tool_errors("WRITE_")
                self._clear_tool_errors("EDIT_")
                self._clear_tool_errors("DIRECTORY_")
                self._clear_tool_errors("PATH_")
                self._clear_tool_errors("PLUGIN_")
                self.unresolved_mutation_errors.clear()
            elif tool == "workspace_command":
                self.successful_verifications += 1
                profile = result.public_metadata.get("profile")
                if isinstance(profile, str):
                    self.verification_profiles.add(profile)
                if self.verification_argument_denials > self.verification_recoveries:
                    self.verification_recoveries += 1
                self._clear_tool_errors("COMMAND_")
            return
        if result.error and result.error.code not in self.unresolved_errors:
            self.unresolved_errors.append(result.error.code)
        if (
            result.error
            and (tool in _MUTATION_TOOLS or plugin_mutation)
            and result.error.code not in self.unresolved_mutation_errors
        ):
            self.unresolved_mutation_errors.append(result.error.code)
        if (
            tool == "workspace_command"
            and result.error is not None
            and result.error.code == "COMMAND_ARGUMENT_DENIED"
        ):
            self.verification_argument_denials += 1
        if tool == "workspace_command" and result.error is not None:
            profile = result.public_metadata.get("profile")
            failure = {
                "code": result.error.code,
                **({"profile": profile} if isinstance(profile, str) else {}),
            }
            if failure not in self.verification_failures:
                self.verification_failures.append(failure)

    def validate_completed(self, *, language: str = "same") -> CompletionDecision:
        """Reject completion only when observed tool facts contradict it."""

        if self.successful_observations < 1:
            return CompletionDecision(
                accepted=False,
                reason="OBSERVATION_REQUIRED",
                guidance=(
                    "在报告完成前\uFF0C至少获取一次成功的工作区观察。"
                    if language == "zh"
                    else "Before reporting completion, obtain at least one successful "
                    "workspace observation."
                ),
            )
        if self.mutation_attempts and not self.successful_mutations:
            return CompletionDecision(
                accepted=False,
                reason="MUTATION_NOT_APPLIED",
                guidance=(
                    "已尝试修改工作区\uFF0C但没有一次成功。请修正参数后重试\uFF0C或报告运行未完成。"
                    if language == "zh"
                    else "A workspace mutation was attempted, but none succeeded. Retry "
                    "with corrected arguments or report the run as incomplete."
                ),
            )
        if self.verification_attempts and not self.successful_verifications:
            return CompletionDecision(
                accepted=False,
                reason="VERIFICATION_NOT_RUN",
                guidance=(
                    "已尝试验证项目\uFF0C但没有命令成功。请使用允许的配置重试\uFF0C或报告运行未完成。"
                    if language == "zh"
                    else "Project verification was attempted, but no command succeeded. "
                    "Retry with an allowed profile or report the run as incomplete."
                ),
            )
        unresolved_mutations = list(self.unresolved_mutation_errors)
        if unresolved_mutations:
            return CompletionDecision(
                accepted=False,
                reason="MUTATION_ERROR_UNRESOLVED",
                guidance=(
                    "最近一次工作区写入未成功 ("
                    + ", ".join(unresolved_mutations)
                    + "). 请先解决该写入错误, 再报告完成; 否则报告运行未完成。"
                    if language == "zh"
                    else "The latest workspace mutation did not succeed ("
                    + ", ".join(unresolved_mutations)
                    + "). Resolve the mutation error before reporting completion, "
                    "or report the run as incomplete."
                ),
            )
        return CompletionDecision(accepted=True)

    def public_summary(self) -> dict[str, object]:
        return {
            "observations": self.successful_observations,
            "mutation_attempts": self.mutation_attempts,
            "mutations": self.successful_mutations,
            "verification_attempts": self.verification_attempts,
            "verifications": self.successful_verifications,
            "changed_files": sorted(self.changed_files),
            "unresolved_errors": list(self.unresolved_errors),
            "verification_argument_denials": self.verification_argument_denials,
            "verification_recoveries": self.verification_recoveries,
            "verification_failures": list(self.verification_failures),
            "verification_profiles": sorted(self.verification_profiles),
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
