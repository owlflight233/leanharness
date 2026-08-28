"""In-process single-use approval coordination for privileged tool calls."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from leanharness.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    RunConflictError,
)

ApprovalDecision = Literal["approve", "reject"]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    run_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    summary: str
    parameters: dict[str, object]
    preview: object | None
    requested_at: float


@dataclass(slots=True)
class _PendingApproval:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalDecision]
    decision: ApprovalDecision | None = None
    expired: bool = False


class ApprovalCoordinator:
    def __init__(
        self,
        *,
        timeout_seconds: float = 15 * 60,
        on_request: Callable[[ApprovalRequest], None] | None = None,
        on_resolve: Callable[[ApprovalRequest, ApprovalDecision], None] | None = None,
        on_expire: Callable[[ApprovalRequest], None] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._on_request = on_request
        self._on_resolve = on_resolve
        self._on_expire = on_expire
        self._items: dict[str, _PendingApproval] = {}

    def request(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        summary: str,
        parameters: dict[str, object],
        preview: object | None,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=str(uuid.uuid4()),
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            summary=summary,
            parameters=parameters,
            preview=preview,
            requested_at=monotonic(),
        )
        pending = _PendingApproval(request, asyncio.get_running_loop().create_future())
        self._items[request.id] = pending
        if self._on_request:
            self._on_request(request)
        return request

    async def wait(self, request: ApprovalRequest) -> ApprovalDecision:
        pending = self._items.get(request.id)
        if pending is None:
            raise ApprovalNotFoundError("Approval request was not found")
        remaining = self.timeout_seconds - (monotonic() - request.requested_at)
        if remaining <= 0:
            self._expire(pending)
            raise ApprovalExpiredError("Approval request expired")
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=remaining)
        except TimeoutError as exc:
            self._expire(pending)
            raise ApprovalExpiredError("Approval request expired") from exc

    def resolve(
        self, run_id: str, approval_id: str, decision: ApprovalDecision
    ) -> ApprovalRequest:
        pending = self._items.get(approval_id)
        if pending is None or pending.request.run_id != run_id:
            raise ApprovalNotFoundError("Approval request was not found")
        if pending.decision is not None:
            raise ApprovalAlreadyResolvedError("Approval request was already resolved")
        if pending.expired or monotonic() - pending.request.requested_at >= self.timeout_seconds:
            self._expire(pending)
            raise ApprovalExpiredError("Approval request expired")
        if self._on_resolve:
            self._on_resolve(pending.request, decision)
        pending.decision = decision
        pending.future.set_result(decision)
        return pending.request

    def cancel_run(self, run_id: str) -> None:
        for pending in self._items.values():
            if pending.request.run_id == run_id and not pending.future.done():
                pending.future.cancel()

    def _expire(self, pending: _PendingApproval) -> None:
        if pending.expired or pending.decision is not None:
            return
        pending.expired = True
        if self._on_expire:
            self._on_expire(pending.request)


class ActiveRunRegistry:
    """Fail closed when two runs target the same local session."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def acquire(self, session_id: str, run_id: str) -> None:
        if session_id in self._sessions:
            raise RunConflictError("This session already has an active run")
        self._sessions[session_id] = run_id

    def assert_available(self, session_id: str) -> None:
        if session_id in self._sessions:
            raise RunConflictError("This session already has an active run")

    def release(self, session_id: str, run_id: str) -> None:
        if self._sessions.get(session_id) == run_id:
            self._sessions.pop(session_id, None)
