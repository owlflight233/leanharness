"""Resolve a new run into an explicit, bounded task intent.

Short continuation messages are references, not new requirements. This
resolver keeps that decision in the application layer so CLI and Web create
the same runtime input and permission snapshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanharness.runtime.completion import TaskRequirements
from leanharness.runtime.continuation import ContinuationContext
from leanharness.storage import LocalStore, RunRecord, SessionRecord

_CONTINUATION_MAX_CHARS = 48
_WHITESPACE = re.compile(r"\s+")

# These expressions are deliberately short and anchored. A message containing
# a concrete new goal (for example, "继续创建登录页面") is a new task.
_CN_CONTINUATION = re.compile(
    r"^(?:继续|你继续(?:执行)?(?:这个)?任务|继续执行(?:这个)?任务|再试试|现在呢|还是不行|"
    r"我(?:再)?换了?(?:个)?权限[,\uFF0C]?\s*(?:再)?试试)$"
)
_EN_CONTINUATIONS = frozenset(
    {"continue", "continue this task", "retry", "retry this task", "try again", "resume"}
)


@dataclass(frozen=True, slots=True)
class ResolvedRunIntent:
    """The public message plus the effective task used by Runtime."""

    original_message: str
    effective_task: str
    source_run_id: str | None
    continued: bool
    requirements: TaskRequirements
    session_permission_mode: str
    continuation: ContinuationContext | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "continued": self.continued,
            "continued_from_run_id": self.source_run_id,
            "session_permission_mode": self.session_permission_mode,
            "requirements": {
                "mutation_required": self.requirements.mutation_required,
                "verification_required": self.requirements.verification_required,
            },
            "effective_task": _summary(self.effective_task),
        }


def is_continuation_message(message: str) -> bool:
    """Return true only for an allow-listed, short follow-up expression."""

    normalized = _WHITESPACE.sub(" ", message.strip()).strip().rstrip("。!?.,")
    normalized = normalized.rstrip("\uFF01\uFF1F\u3002\uFF0C")
    if not normalized or len(normalized) > _CONTINUATION_MAX_CHARS:
        return False
    folded = normalized.casefold()
    return bool(_CN_CONTINUATION.fullmatch(normalized) or folded in _EN_CONTINUATIONS)


def resolve_run_intent(
    store: LocalStore,
    session: SessionRecord,
    message: str,
    *,
    permission_mode: str | None = None,
) -> ResolvedRunIntent:
    """Resolve continuation references against terminal runs in this session."""

    if not is_continuation_message(message):
        return ResolvedRunIntent(
            original_message=message,
            effective_task=message,
            source_run_id=None,
            continued=False,
            requirements=TaskRequirements.infer(message),
            session_permission_mode=session.permission_mode,
        )

    source = latest_substantive_run(store, session)
    if source is None:
        # With no local run to reference, the text is still a normal task and
        # must obtain workspace evidence before it can be marked complete.
        return ResolvedRunIntent(
            original_message=message,
            effective_task=message,
            source_run_id=None,
            continued=False,
            requirements=TaskRequirements.infer(message),
            session_permission_mode=session.permission_mode,
        )

    requirements = TaskRequirements.infer(source.task)
    context = continuation_for_run(
        store,
        session,
        source,
        requirements=requirements,
        current_permission_mode=permission_mode,
    )
    return ResolvedRunIntent(
        original_message=message,
        effective_task=source.task,
        source_run_id=source.id,
        continued=True,
        requirements=requirements,
        session_permission_mode=session.permission_mode,
        continuation=context,
    )


def latest_substantive_run(store: LocalStore, session: SessionRecord) -> RunRecord | None:
    terminal_states = {"COMPLETED", "EXHAUSTED", "FAILED", "CANCELLED"}
    for run in reversed(store.list_runs(session.id)):
        if (
            run.mode in {"coding", "inspect", "plan"}
            and run.state in terminal_states
            and not is_continuation_message(run.task)
        ):
            return run
    return None


def continuation_for_run(
    store: LocalStore,
    session: SessionRecord,
    run: RunRecord,
    *,
    requirements: TaskRequirements | None = None,
    current_permission_mode: str | None = None,
) -> ContinuationContext:
    """Build a bounded capsule from one known terminal run."""

    events = store.list_events(run.id)
    terminal_event = next(
        (
            event
            for event in reversed(events)
            if event.get("type")
            in {"run.completed", "run.incomplete", "run.failed", "run.cancelled"}
        ),
        {},
    )
    metadata = terminal_event.get("metadata")
    evidence = metadata.get("evidence") if isinstance(metadata, dict) else None
    changed = evidence.get("changed_files") if isinstance(evidence, dict) else None
    changed_files = tuple(str(path) for path in changed[:50]) if isinstance(changed, list) else ()
    reason = run.error_code
    if reason is None and isinstance(metadata, dict):
        candidate = metadata.get("incomplete_reason")
        reason = str(candidate) if candidate else None
    selected = requirements or TaskRequirements.infer(run.task)
    return ContinuationContext(
        previous_task=run.task,
        previous_state=run.state,
        changed_files=changed_files,
        incomplete_reason=reason,
        permission_mode=current_permission_mode or session.permission_mode,
        previous_run_permission_mode=run.permission_mode,
        source_run_id=run.id,
        mutation_required=selected.mutation_required,
        verification_required=selected.verification_required,
    )


def _summary(value: str) -> str:
    normalized = " ".join(value.strip().split())
    return normalized[:200]
