"""Fail-closed permission decisions for runtime tool actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PermissionMode(StrEnum):
    INSPECT = "inspect"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    code: str


INSPECT_TOOLS = frozenset({"workspace_list", "workspace_read", "workspace_search"})


def authorize_tool(mode: PermissionMode, tool_name: str) -> PermissionDecision:
    if mode is PermissionMode.INSPECT and tool_name in INSPECT_TOOLS:
        return PermissionDecision(allowed=True, code="TOOL_ALLOWED")
    return PermissionDecision(allowed=False, code="TOOL_PERMISSION_DENIED")
