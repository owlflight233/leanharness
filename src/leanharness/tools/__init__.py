"""Built-in tool contracts and guarded execution."""

from leanharness.tools.contracts import ToolErrorInfo, ToolExecutionError, ToolResult
from leanharness.tools.controlled import (
    GitInspectTool,
    WorkspaceCommandTool,
    WorkspaceEditTool,
    WorkspaceMkdirTool,
    WorkspacePatchTool,
    WorkspaceWriteTool,
)
from leanharness.tools.registry import ToolRegistry

__all__ = [
    "GitInspectTool",
    "ToolErrorInfo",
    "ToolExecutionError",
    "ToolRegistry",
    "ToolResult",
    "WorkspaceCommandTool",
    "WorkspaceEditTool",
    "WorkspaceMkdirTool",
    "WorkspacePatchTool",
    "WorkspaceWriteTool",
]
