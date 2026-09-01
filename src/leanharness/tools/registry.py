"""Permission-aware registry for built-in runtime tools."""

from __future__ import annotations

from pathlib import Path

from leanharness.models import ToolCall, ToolDefinition
from leanharness.permissions.policy import PermissionMode, authorize_tool
from leanharness.tools.contracts import BuiltinTool, ToolErrorInfo, ToolExecutionError, ToolResult
from leanharness.tools.controlled import (
    CancellationSignal,
    GitInspectTool,
    WorkspaceCommandTool,
    WorkspaceEditTool,
    WorkspaceMkdirTool,
    WorkspacePatchTool,
    WorkspaceWriteTool,
)
from leanharness.tools.workspace import (
    WorkspaceBoundary,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceSearchTool,
)


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        *,
        mode: PermissionMode = PermissionMode.INSPECT,
        additional_tools: tuple[BuiltinTool, ...] = (),
    ) -> None:
        boundary = WorkspaceBoundary.create(workspace)
        builtins: tuple[BuiltinTool, ...] = (
            WorkspaceListTool(boundary),
            WorkspaceReadTool(boundary),
            WorkspaceSearchTool(boundary),
            GitInspectTool(boundary),
            WorkspaceMkdirTool(boundary),
            WorkspacePatchTool(boundary),
            WorkspaceWriteTool(boundary),
            WorkspaceEditTool(boundary),
            WorkspaceCommandTool(boundary),
        )
        self._mode = mode
        self._tools: dict[str, BuiltinTool] = {
            tool.definition.name: tool
            for tool in builtins
            if authorize_tool(mode, tool.definition.name).allowed
        }
        for tool in additional_tools:
            name = tool.definition.name
            if name in self._tools or any(item.definition.name == name for item in builtins):
                raise ValueError(f"Duplicate tool name: {name}")
            mutation = bool(getattr(tool, "is_mutating", False))
            if authorize_tool(mode, name, mutation=mutation).allowed:
                self._tools[name] = tool

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())

    def execute(
        self,
        call: ToolCall,
        *,
        cancel_signal: CancellationSignal | None = None,
    ) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return _error_result(call, ToolExecutionError("TOOL_NOT_FOUND", "Unknown tool"))
        decision = self._decision(call.name, tool)
        if not decision.allowed:
            return _error_result(
                call,
                ToolExecutionError(decision.code, "Tool is not allowed in inspect mode"),
            )
        try:
            if isinstance(tool, WorkspaceCommandTool) or bool(
                getattr(tool, "supports_cancellation", False)
            ):
                return tool.execute(call.id, call.arguments, cancel_signal=cancel_signal)
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

    def approval_required(self, call: ToolCall) -> bool:
        tool = self._tools.get(call.name)
        if tool is None:
            return False
        return self._decision(call.name, tool).requires_approval

    def preview(self, call: ToolCall) -> dict[str, object]:
        tool = self._tools.get(call.name)
        if tool is None:
            raise ToolExecutionError("TOOL_NOT_FOUND", "Unknown tool")
        preview = getattr(tool, "preview", None)
        if preview is None:
            return {"tool": call.name}
        return preview(call.arguments)

    def execute_approved(
        self,
        call: ToolCall,
        *,
        expected_hashes: dict[str, str | None] | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return _error_result(call, ToolExecutionError("TOOL_NOT_FOUND", "Unknown tool"))
        try:
            if isinstance(tool, WorkspacePatchTool):
                return tool.execute(call.id, call.arguments, expected_hashes=expected_hashes)
            if isinstance(tool, WorkspaceCommandTool):
                return tool.execute(call.id, call.arguments, cancel_signal=cancel_signal)
            if bool(getattr(tool, "supports_cancellation", False)):
                return tool.execute(call.id, call.arguments, cancel_signal=cancel_signal)
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

    def _decision(self, name: str, tool: BuiltinTool):
        mutation = getattr(tool, "is_mutating", None)
        return authorize_tool(
            self._mode,
            name,
            mutation=mutation if isinstance(mutation, bool) else None,
        )


def _error_result(call: ToolCall, exc: ToolExecutionError) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id,
        tool=call.name,
        ok=False,
        error=ToolErrorInfo(exc.code, exc.message, exc.recoverable),
        public_metadata={"error_code": exc.code, "recoverable": exc.recoverable},
    )
