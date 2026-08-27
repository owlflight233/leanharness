"""Built-in tool contracts and guarded execution."""

from leanharness.tools.contracts import ToolErrorInfo, ToolExecutionError, ToolResult
from leanharness.tools.controlled import GitInspectTool, WorkspaceCommandTool, WorkspacePatchTool
from leanharness.tools.registry import ToolRegistry

__all__ = [
    "GitInspectTool",
    "ToolErrorInfo",
    "ToolExecutionError",
    "ToolRegistry",
    "ToolResult",
    "WorkspaceCommandTool",
    "WorkspacePatchTool",
]
