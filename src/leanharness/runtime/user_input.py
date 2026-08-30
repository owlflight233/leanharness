"""Model-requested, single-use user input coordination for one active run."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from time import monotonic

from leanharness.models import ToolCall, ToolDefinition

REQUEST_USER_INPUT_TOOL_NAME = "request_user_input"
REQUEST_USER_INPUT_TOOL = ToolDefinition(
    name=REQUEST_USER_INPUT_TOOL_NAME,
    description=(
        "Ask the user one necessary clarifying question. Use only when the task cannot be "
        "safely continued from repository evidence. Provide two or three concise options."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 500},
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1, "maxLength": 80},
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                    },
                    "required": ["label", "description"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["question", "options"],
        "additionalProperties": False,
    },
)


class UserInputProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UserInputExpiredError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UserInputOption:
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class UserInputRequest:
    id: str
    run_id: str
    session_id: str
    tool_call_id: str
    question: str
    options: tuple[UserInputOption, ...]
    requested_at: float


@dataclass(slots=True)
class _PendingInput:
    request: UserInputRequest
    future: asyncio.Future[str]
    answer: str | None = None
    expired: bool = False


class UserInputCoordinator:
    def __init__(self, *, timeout_seconds: float = 15 * 60) -> None:
        self.timeout_seconds = timeout_seconds
        self._items: dict[str, _PendingInput] = {}

    def request(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        question: str,
        options: tuple[UserInputOption, ...],
    ) -> UserInputRequest:
        request = UserInputRequest(
            id=str(uuid.uuid4()),
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            question=question,
            options=options,
            requested_at=monotonic(),
        )
        self._items[request.id] = _PendingInput(
            request=request,
            future=asyncio.get_running_loop().create_future(),
        )
        return request

    async def wait(self, request: UserInputRequest) -> str:
        pending = self._items.get(request.id)
        if pending is None:
            raise UserInputProtocolError("INPUT_NOT_FOUND", "Input request was not found")
        remaining = self.timeout_seconds - (monotonic() - request.requested_at)
        if remaining <= 0:
            pending.expired = True
            raise UserInputExpiredError("Input request expired")
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=remaining)
        except TimeoutError as exc:
            pending.expired = True
            raise UserInputExpiredError("Input request expired") from exc

    def resolve(self, run_id: str, input_id: str, answer: str) -> UserInputRequest:
        pending = self._items.get(input_id)
        if pending is None or pending.request.run_id != run_id:
            raise UserInputProtocolError("INPUT_NOT_FOUND", "Input request was not found")
        if pending.answer is not None:
            raise UserInputProtocolError("INPUT_ALREADY_RESOLVED", "Input was already resolved")
        if pending.expired or monotonic() - pending.request.requested_at >= self.timeout_seconds:
            pending.expired = True
            raise UserInputExpiredError("Input request expired")
        normalized = answer.strip() if isinstance(answer, str) else ""
        if not normalized or len(normalized) > 2_000:
            raise UserInputProtocolError(
                "INPUT_INVALID_ANSWER", "Answer must contain 1 to 2000 characters"
            )
        pending.answer = normalized
        pending.future.set_result(normalized)
        return pending.request

    def cancel_run(self, run_id: str) -> None:
        for pending in self._items.values():
            if pending.request.run_id == run_id and not pending.future.done():
                pending.future.cancel()


def parse_user_input_call(call: ToolCall) -> tuple[str, tuple[UserInputOption, ...]]:
    if set(call.arguments) != {"question", "options"}:
        raise UserInputProtocolError(
            "INPUT_INVALID_FORMAT", "Input request has unsupported or missing fields"
        )
    question = call.arguments.get("question")
    raw_options = call.arguments.get("options")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
        raise UserInputProtocolError(
            "INPUT_INVALID_FORMAT", "Question must contain 1 to 500 characters"
        )
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 3:
        raise UserInputProtocolError(
            "INPUT_INVALID_FORMAT", "Input request must contain two or three options"
        )
    options = []
    for raw in raw_options:
        if not isinstance(raw, dict) or set(raw) != {"label", "description"}:
            raise UserInputProtocolError("INPUT_INVALID_FORMAT", "Input option is invalid")
        label = raw.get("label")
        description = raw.get("description")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise UserInputProtocolError("INPUT_INVALID_FORMAT", "Input option label is invalid")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 240:
            raise UserInputProtocolError(
                "INPUT_INVALID_FORMAT", "Input option description is invalid"
            )
        options.append(UserInputOption(label.strip(), description.strip()))
    return question.strip(), tuple(options)
