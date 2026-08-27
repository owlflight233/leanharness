"""Typed contracts shared by built-in tools and the runtime dispatcher."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from leanharness.models import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolErrorInfo:
    code: str
    message: str
    recoverable: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    tool: str
    ok: bool
    data: dict[str, Any] | list[Any] | str | None = None
    error: ToolErrorInfo | None = None
    public_metadata: dict[str, object] = field(default_factory=dict)

    def to_model_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"ok": self.ok, "tool": self.tool}
        if self.ok:
            payload["result"] = self.data
        elif self.error is not None:
            payload["error"] = self.error.to_dict()
            if self.data is not None:
                payload["result"] = self.data
        return payload

    def to_model_content(self) -> str:
        return json.dumps(self.to_model_dict(), ensure_ascii=False, separators=(",", ":"))


class BuiltinTool(Protocol):
    definition: ToolDefinition

    def execute(self, tool_call_id: str, arguments: dict[str, Any]) -> ToolResult: ...


class ToolExecutionError(Exception):
    def __init__(self, code: str, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
