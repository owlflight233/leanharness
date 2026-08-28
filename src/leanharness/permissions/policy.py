"""Fail-closed permission decisions for runtime tool actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PermissionMode(StrEnum):
    INSPECT = "inspect"
    APPROVE = "approve"
    UNRESTRICTED = "unrestricted"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    code: str
    requires_approval: bool = False


INSPECT_TOOLS = frozenset(
    {"workspace_list", "workspace_read", "workspace_search", "git_inspect"}
)
MUTATING_TOOLS = frozenset({"workspace_mkdir", "workspace_patch", "workspace_command"})


def authorize_tool(mode: PermissionMode, tool_name: str) -> PermissionDecision:
    if tool_name in INSPECT_TOOLS:
        return PermissionDecision(allowed=True, code="TOOL_ALLOWED")
    if tool_name in MUTATING_TOOLS:
        if mode is PermissionMode.APPROVE:
            return PermissionDecision(
                allowed=True, code="TOOL_APPROVAL_REQUIRED", requires_approval=True
            )
        if mode is PermissionMode.UNRESTRICTED:
            return PermissionDecision(allowed=True, code="TOOL_ALLOWED")
    return PermissionDecision(allowed=False, code="TOOL_PERMISSION_DENIED")
