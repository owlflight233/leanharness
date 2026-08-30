"""Run evidence used to validate a model-owned terminal decision."""

from __future__ import annotations

from dataclasses import dataclass, field

from leanharness.tools import ToolResult

_OBSERVATION_TOOLS = frozenset(
    {"workspace_list", "workspace_read", "workspace_search", "git_inspect"}
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

    def record(self, tool: str, result: ToolResult) -> None:
        if tool in {"workspace_mkdir", "workspace_patch"}:
            self.mutation_attempts += 1
        elif tool == "workspace_command":
            self.verification_attempts += 1
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

    def validate_completed(self) -> CompletionDecision:
        """Reject completion only when observed tool facts contradict it."""

        if self.mutation_attempts and not self.successful_mutations:
            return CompletionDecision(
                accepted=False,
                reason="MUTATION_NOT_APPLIED",
                guidance=(
                    "A workspace mutation was attempted, but none succeeded. Retry with "
                    "corrected arguments or report the run as incomplete."
                ),
            )
        if self.verification_attempts and not self.successful_verifications:
            return CompletionDecision(
                accepted=False,
                reason="VERIFICATION_NOT_RUN",
                guidance=(
                    "Project verification was attempted, but no command succeeded. Retry "
                    "with an allowed profile or report the run as incomplete."
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
