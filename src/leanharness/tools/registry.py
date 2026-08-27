"""Permission-aware registry for built-in runtime tools."""

from __future__ import annotations

from pathlib import Path

from leanharness.models import ToolCall, ToolDefinition
from leanharness.permissions.policy import PermissionMode, authorize_tool
from leanharness.tools.contracts import BuiltinTool, ToolErrorInfo, ToolExecutionError, ToolResult
from leanharness.tools.workspace import (
    WorkspaceBoundary,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceSearchTool,
)


class ToolRegistry:
    def __init__(self, workspace: Path, *, mode: PermissionMode = PermissionMode.INSPECT) -> None:
        boundary = WorkspaceBoundary.create(workspace)
        builtins: tuple[BuiltinTool, ...] = (
            WorkspaceListTool(boundary),
            WorkspaceReadTool(boundary),
            WorkspaceSearchTool(boundary),
        )
        self._tools = {tool.definition.name: tool for tool in builtins}
        self._mode = mode

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return _error_result(call, ToolExecutionError("TOOL_NOT_FOUND", "Unknown tool"))
        decision = authorize_tool(self._mode, call.name)
        if not decision.allowed:
            return _error_result(
                call,
                ToolExecutionError(decision.code, "Tool is not allowed in inspect mode"),
            )
        try:
            return tool.execute(call.id, call.arguments)
        except ToolExecutionError as exc:
            return _error_result(call, exc)
        except Exception:
            return _error_result(
                call,
                ToolExecutionError(
                    "TOOL_EXECUTION_FAILED",
                    "Tool execution failed safely",
                    recoverable=False,
                ),
            )


def _error_result(call: ToolCall, exc: ToolExecutionError) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id,
        tool=call.name,
        ok=False,
        error=ToolErrorInfo(exc.code, exc.message, exc.recoverable),
        public_metadata={"error_code": exc.code, "recoverable": exc.recoverable},
    )
