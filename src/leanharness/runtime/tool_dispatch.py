"""Runtime-independent preparation and execution of one registered tool call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from leanharness.models import ToolCall
from leanharness.tools import (
    ToolErrorInfo,
    ToolExecutionError,
    ToolRegistry,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class ApprovalPreview:
    parameters: dict[str, object]
    preview: object | None
    expected_hashes: dict[str, str | None] | None


class ToolDispatcher:
    """Translate registry operations into safe results without owning run policy."""

    def __init__(self, registry: ToolRegistry, cancel_event: asyncio.Event) -> None:
        self._registry = registry
        self._cancel_event = cancel_event

    def prepare_approval(self, call: ToolCall) -> ApprovalPreview | ToolResult:
        try:
            preview_data = self._registry.preview(call)
        except ToolExecutionError as exc:
            return tool_error_result(
                call,
                exc.code,
                exc.message,
                recoverable=exc.recoverable,
            )
        except Exception:
            return tool_error_result(
                call,
                "APPROVAL_PREVIEW_FAILED",
                "A safe approval preview could not be created",
            )
        expected_hashes = preview_data.pop("target_hashes", None)
        raw_preview = preview_data.pop("preview", None)
        return ApprovalPreview(
            parameters=preview_data,
            preview=raw_preview,
            expected_hashes=(
                expected_hashes if isinstance(expected_hashes, dict) else None
            ),
        )

    async def execute(
        self,
        call: ToolCall,
        *,
        approved: bool = False,
        expected_hashes: dict[str, str | None] | None = None,
    ) -> ToolResult:
        try:
            if approved:
                return await asyncio.to_thread(
                    self._registry.execute_approved,
                    call,
                    expected_hashes=expected_hashes,
                    cancel_signal=self._cancel_event,
                )
            return await asyncio.to_thread(
                self._registry.execute,
                call,
                cancel_signal=self._cancel_event,
            )
        except asyncio.CancelledError:
            self._cancel_event.set()
            raise


def tool_error_result(
    call: ToolCall,
    code: str,
    message: str,
    *,
    recoverable: bool = True,
) -> ToolResult:
    return ToolResult(
        tool_call_id=call.id,
        tool=call.name,
        ok=False,
        error=ToolErrorInfo(code=code, message=message, recoverable=recoverable),
        public_metadata={"error_code": code, "recoverable": recoverable},
    )
