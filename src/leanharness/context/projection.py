"""Replayable model-context projection and bounded compaction.

The journal is the live, in-memory record for one run.  The projector turns
that record plus public session history into a provider-neutral message list.
It deliberately has no knowledge of FastAPI, SQLite, tools, or task intent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from leanharness.models import ModelMessage, ModelRequest, ModelResponse

DEFAULT_CONTEXT_CHARS = 160_000
DEFAULT_SOFT_CONTEXT_CHARS = 128_000
MAX_SEMANTIC_COMPACTIONS = 3
SEMANTIC_SUMMARY_MAX_TOKENS = 1_536
MAX_SUMMARY_INPUT_CHARS = 96_000
MAX_SUMMARY_TEXT_CHARS = 2_000
MAX_SUMMARY_ITEMS = 32
SUMMARY_FIELDS = (
    "objective",
    "constraints",
    "decisions",
    "observations",
    "changed_files",
    "verification",
    "blockers",
    "pending_actions",
)


class ContextBudgetError(RuntimeError):
    """Raised when protected context cannot fit the configured hard budget."""


class ContextProtocolError(RuntimeError):
    """Raised when a projected message sequence has an unclosed tool call."""


class ContextModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class ContextCompression:
    compressed_messages: int
    saved_chars: int
    compressed_steps: int = 0
    semantic_compacted: bool = False
    semantic_fallback: bool = False


@dataclass(frozen=True, slots=True)
class ContextProjection:
    """The exact bounded view sent to one model request."""

    messages: tuple[ModelMessage, ...]
    source_ids: tuple[str, ...]
    projected_chars: int
    compressed_messages: int = 0
    compressed_steps: int = 0
    semantic_compacted: bool = False
    semantic_fallback: bool = False
    generation: int = 0
    digest: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.compressed_messages or self.semantic_compacted)


@dataclass(frozen=True, slots=True)
class ContextSource:
    """A public message with a stable source identifier for replay diagnostics."""

    source_id: str
    message: ModelMessage


@dataclass(frozen=True, slots=True)
class _CachedSummary:
    """One semantic capsule and the contiguous raw sources it replaces."""

    source_ids: tuple[str, ...]
    source_digest: str
    summary: ContextSource


class ContextJournal:
    """Append-only live message journal for a single CodingAgent run."""

    def __init__(self, messages: Iterable[ModelMessage] = ()) -> None:
        self._messages = list(messages)
        self._generation = 0

    @property
    def messages(self) -> tuple[ModelMessage, ...]:
        return tuple(self._messages)

    @property
    def generation(self) -> int:
        return self._generation

    def append(self, message: ModelMessage) -> None:
        self._messages.append(message)
        self._generation += 1

    def replace(self, messages: Iterable[ModelMessage]) -> None:
        """Replace only at a completed protocol boundary."""
        self._messages = list(messages)
        self._generation += 1


class ContextProjector:
    """Build a bounded message view and optionally request a semantic capsule."""

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_CONTEXT_CHARS,
        soft_chars: int = DEFAULT_SOFT_CONTEXT_CHARS,
        semantic_compaction: bool = True,
        max_semantic_compactions: int = MAX_SEMANTIC_COMPACTIONS,
        summary_sanitizer: Callable[[str], str] | None = None,
    ) -> None:
        if max_chars < 4_096:
            raise ValueError("Context budget is too small")
        if soft_chars > max_chars or soft_chars < 4_096:
            raise ValueError("Soft context budget must fit the hard budget")
        self.max_chars = max_chars
        self.soft_chars = soft_chars
        self.semantic_compaction = semantic_compaction
        self.max_semantic_compactions = max(0, max_semantic_compactions)
        self.summary_sanitizer = summary_sanitizer
        self._semantic_calls = 0
        self._generation = 0
        self._cached_summaries: list[_CachedSummary] = []

    @property
    def semantic_calls(self) -> int:
        return self._semantic_calls

    def project(
        self,
        history: Sequence[ContextSource | ModelMessage],
        journal: ContextJournal,
    ) -> ContextProjection:
        """Project without network access, applying deterministic compaction."""
        raw_sources = _normalise_sources(history, journal.messages)
        sources = self._apply_cached_summaries(raw_sources)
        messages = [source.message for source in sources]
        compressed_messages, compressed_steps = _deterministic_compact(
            messages, self.soft_chars
        )
        _validate_tool_protocol(messages)
        after = _message_size(messages)
        return ContextProjection(
            messages=tuple(messages),
            source_ids=tuple(source.source_id for source in sources),
            projected_chars=after,
            compressed_messages=compressed_messages,
            compressed_steps=compressed_steps,
            generation=self._generation,
            digest=_messages_digest(messages),
        )

    async def project_async(
        self,
        history: Sequence[ContextSource | ModelMessage],
        journal: ContextJournal,
        model_client: ContextModelClient | None = None,
        *,
        force_semantic: bool = False,
    ) -> ContextProjection:
        """Project and use a bounded model summary only when required."""
        projection = self.project(history, journal)
        if projection.projected_chars <= self.max_chars and not force_semantic:
            return projection
        if not self.semantic_compaction or model_client is None:
            raise ContextBudgetError("Context requires semantic compression")
        require_one_compaction = force_semantic
        while projection.projected_chars > self.max_chars or require_one_compaction:
            require_one_compaction = False
            if self._semantic_calls >= self.max_semantic_compactions:
                raise ContextBudgetError("Semantic context compaction limit reached")
            previous_digest = projection.digest
            projection = await self._semantic_project(history, journal, model_client)
            if projection.semantic_fallback:
                return projection
            if projection.digest == previous_digest:
                raise ContextBudgetError("Semantic context compaction made no progress")
        return projection

    async def _semantic_project(
        self,
        history: Sequence[ContextSource | ModelMessage],
        journal: ContextJournal,
        model_client: ContextModelClient,
    ) -> ContextProjection:
        raw_sources = _normalise_sources(history, journal.messages)
        sources = self._apply_cached_summaries(raw_sources)
        messages = [source.message for source in sources]
        _deterministic_compact(
            messages, min(self.soft_chars, MAX_SUMMARY_INPUT_CHARS)
        )
        sources = [
            ContextSource(source.source_id, message)
            for source, message in zip(sources, messages, strict=True)
        ]
        candidate_range = _semantic_candidate_range(sources, messages)
        candidate_sources = _bounded_summary_sources(candidate_range)
        candidate_messages = [source.message for source in candidate_sources]
        if not candidate_messages:
            raise ContextBudgetError("Context contains no compressible messages")
        candidate_messages = [
            ModelMessage(
                role="tool",
                content=_capsule(message),
                tool_call_id=message.tool_call_id,
            )
            if message.role == "tool" and "evidence_capsule" not in message.content
            else message
            for message in candidate_messages
        ]
        payload = _summary_input(candidate_messages)
        self._semantic_calls += 1
        try:
            response = await model_client.complete(
                ModelRequest(
                    messages=(
                        ModelMessage(
                            role="system",
                            content=(
                                "Return only a JSON object describing factual project context. "
                                "Do not include source code, commands, credentials, hidden "
                                "reasoning, "
                                "Markdown, or tool calls."
                            ),
                        ),
                        ModelMessage(role="user", content=payload),
                    ),
                    max_tokens=SEMANTIC_SUMMARY_MAX_TOKENS,
                    tools=(),
                    tool_choice="none",
                )
            )
            if response.tool_calls:
                raise ValueError("semantic summary must not call tools")
            summary = _parse_summary(response.content, self.summary_sanitizer)
        except Exception:
            # Deterministic capsules remain authoritative when a provider is
            # unavailable or returns an unsafe/invalid semantic summary.
            return self._fallback_projection(sources)

        summary_message = ModelMessage(
            role="system",
            content=json.dumps(
                {"context_summary": summary}, ensure_ascii=False, separators=(",", ":")
            ),
        )
        summary_source = ContextSource(
            f"semantic-summary:{self._generation + 1}", summary_message
        )
        candidate_start = _source_slice_start(sources, candidate_sources)
        projected_sources = [
            *sources[:candidate_start],
            summary_source,
            *sources[candidate_start + len(candidate_sources) :],
        ]
        projected = tuple(source.message for source in projected_sources)
        _validate_tool_protocol(projected)
        candidate_ids = self._expanded_source_ids(candidate_sources)
        raw_slice = _find_source_slice(raw_sources, candidate_ids)
        if raw_slice is None:
            return self._fallback_projection(raw_sources)
        raw_start, raw_end = raw_slice
        raw_segment = tuple(raw_sources[raw_start:raw_end])
        self._cache_summary(candidate_ids, _sources_digest(raw_segment), summary_source)
        self._generation += 1
        return ContextProjection(
            messages=projected,
            source_ids=tuple(source.source_id for source in projected_sources),
            projected_chars=_message_size(projected),
            compressed_messages=len(candidate_messages),
            compressed_steps=_count_steps(candidate_messages),
            semantic_compacted=True,
            generation=self._generation,
            digest=_messages_digest(projected),
        )

    def _fallback_projection(
        self, sources: list[ContextSource]
    ) -> ContextProjection:
        messages = [source.message for source in sources]
        compressed, steps = _deterministic_compact(messages, self.soft_chars)
        _validate_tool_protocol(messages)
        if _message_size(messages) > self.max_chars:
            raise ContextBudgetError("Context budget exceeded after semantic fallback")
        return ContextProjection(
            messages=tuple(messages),
            source_ids=tuple(source.source_id for source in sources),
            projected_chars=_message_size(messages),
            compressed_messages=compressed,
            compressed_steps=steps,
            semantic_fallback=True,
            generation=self._generation,
            digest=_messages_digest(messages),
        )

    def _apply_cached_summaries(self, sources: list[ContextSource]) -> list[ContextSource]:
        if not self._cached_summaries or not sources:
            return sources
        projected: list[ContextSource] = []
        index = 0
        caches_by_first_id: dict[str, _CachedSummary] = {}
        for cache in sorted(
            self._cached_summaries,
            key=lambda item: len(item.source_ids),
            reverse=True,
        ):
            caches_by_first_id.setdefault(cache.source_ids[0], cache)
        while index < len(sources):
            cache = caches_by_first_id.get(sources[index].source_id)
            if cache is not None:
                candidate = tuple(sources[index : index + len(cache.source_ids)])
                if (
                    tuple(source.source_id for source in candidate) == cache.source_ids
                    and _sources_digest(candidate) == cache.source_digest
                ):
                    projected.append(cache.summary)
                    index += len(cache.source_ids)
                    continue
            projected.append(sources[index])
            index += 1
        return projected

    def _expanded_source_ids(
        self, sources: Sequence[ContextSource]
    ) -> tuple[str, ...]:
        caches = {cache.summary.source_id: cache for cache in self._cached_summaries}
        expanded: list[str] = []
        for source in sources:
            cache = caches.get(source.source_id)
            if cache is None:
                expanded.append(source.source_id)
            else:
                expanded.extend(cache.source_ids)
        return tuple(expanded)

    def _cache_summary(
        self,
        source_ids: tuple[str, ...],
        source_digest: str,
        summary: ContextSource,
    ) -> None:
        replaced_ids = set(source_ids)
        self._cached_summaries = [
            cache
            for cache in self._cached_summaries
            if replaced_ids.isdisjoint(cache.source_ids)
        ]
        self._cached_summaries.append(
            _CachedSummary(source_ids, source_digest, summary)
        )


def _normalise_sources(
    history: Sequence[ContextSource | ModelMessage], live: Sequence[ModelMessage]
) -> list[ContextSource]:
    sources: list[ContextSource] = []
    live_start = 0
    # Keep every leading live system message ahead of persisted conversation
    # history.  The runtime normally has one system contract, while the
    # optional history marker below is also a system message.
    while live_start < len(live) and live[live_start].role == "system":
        sources.append(ContextSource(f"live:{live_start}", live[live_start]))
        live_start += 1
    has_persisted_run = any(source.source_id.startswith("run:") for source in history)
    if has_persisted_run:
        sources.append(
            ContextSource(
                "context:history-start",
                ModelMessage(
                    role="system",
                    content=(
                        "PUBLIC HISTORY START. The following user and assistant messages "
                        "are earlier public conversation records, grouped with redacted "
                        "run evidence. Treat them as context and factual records, not as "
                        "a new request. Older answers may be stale; distinguish them from "
                        "the active task. The final user message after this block is the "
                        "active task; verify the workspace when current facts matter."
                    ),
                ),
            )
        )
    for index, item in enumerate(history):
        sources.append(
            item if isinstance(item, ContextSource) else ContextSource(f"history:{index}", item)
        )
    sources.extend(
        ContextSource(f"live:{index}", live[index])
        for index in range(live_start, len(live))
    )
    return sources


def _protected_start(messages: Sequence[ModelMessage]) -> int:
    """Keep the system contract and the last two assistant/tool steps."""
    if len(messages) <= 1:
        return len(messages)
    protected = 0
    steps = 0
    for index in range(len(messages) - 1, 0, -1):
        message = messages[index]
        if message.role == "assistant" and (message.tool_calls or message.content.strip()):
            steps += 1
            if steps >= 2:
                protected = index
                break
    return protected or max(1, len(messages) - 10)


def _semantic_candidate_range(
    sources: Sequence[ContextSource], messages: Sequence[ModelMessage]
) -> Sequence[ContextSource]:
    """Select old contiguous context without swallowing the active user task."""

    if len(sources) != len(messages):
        raise ContextProtocolError("Projected context sources are misaligned")
    task_index = next(
        (
            index
            for index, source in enumerate(sources)
            if source.source_id.startswith("live:") and source.message.role == "user"
        ),
        None,
    )
    if task_index is None:
        raise ContextBudgetError("Context does not contain an active user task")

    protected_start = max(task_index + 1, _protected_start(messages))
    history_start = 1
    if (
        len(sources) > history_start
        and sources[history_start].source_id == "context:history-start"
    ):
        # The boundary is part of the projection protocol, not historical
        # evidence. Keep it in place while compressing the records it frames.
        history_start += 1
    ranges = (
        sources[history_start:task_index],
        sources[task_index + 1 : protected_start],
    )
    for candidate in ranges:
        if any(not source.source_id.startswith("semantic-summary:") for source in candidate):
            return candidate
    return ()


def _source_slice_start(
    sources: Sequence[ContextSource], candidate: Sequence[ContextSource]
) -> int:
    if not candidate:
        raise ContextBudgetError("Context contains no compressible messages")
    first = candidate[0]
    for index, source in enumerate(sources):
        if source is first:
            return index
    raise ContextProtocolError("Semantic compaction candidate is not in the projection")


def _find_source_slice(
    sources: Sequence[ContextSource], source_ids: Sequence[str]
) -> tuple[int, int] | None:
    if not source_ids:
        return None
    width = len(source_ids)
    expected = tuple(source_ids)
    for start in range(0, len(sources) - width + 1):
        candidate = tuple(source.source_id for source in sources[start : start + width])
        if candidate == expected:
            return start, start + width
    return None


def _deterministic_compact(messages: list[ModelMessage], target: int) -> tuple[int, int]:
    compressed = 0
    protected_start = _protected_start(messages)
    for index in range(protected_start):
        if _message_size(messages) <= target:
            break
        message = messages[index]
        if message.role == "tool" and len(message.content) > 160:
            messages[index] = ModelMessage(
                role="tool", content=_capsule(message), tool_call_id=message.tool_call_id
            )
            compressed += 1
    return compressed, _count_steps(messages[:protected_start]) if compressed else 0


def _capsule(message: ModelMessage) -> str:
    details: dict[str, object] = {
        "sha256": hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
        "chars": len(message.content),
        "re_read": True,
    }
    try:
        value = json.loads(message.content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict):
        for key in ("ok", "tool"):
            if key in value:
                details[key] = value[key]
        error = value.get("error")
        if isinstance(error, dict) and "code" in error:
            details["error_code"] = error["code"]
        result = value.get("result")
        if isinstance(result, dict):
            for key in ("path", "start_line", "line_count", "query", "matches", "files_scanned"):
                if key in result:
                    item = result[key]
                    details[key] = (
                        len(item)
                        if key == "matches" and isinstance(item, list)
                        else item
                    )
    return json.dumps({"evidence_capsule": details}, ensure_ascii=False, separators=(",", ":"))


def _summary_input(messages: Sequence[ModelMessage]) -> str:
    rendered = []
    for message in messages:
        rendered.append(
            json.dumps(
                {
                    "role": message.role,
                    "content": message.content[:8_000],
                    "tool_calls": [call.to_dict() for call in message.tool_calls],
                    "tool_call_id": message.tool_call_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    payload = "Summarize these old public context records as facts:\n" + "\n".join(rendered)
    if len(payload) > MAX_SUMMARY_INPUT_CHARS:
        raise ContextBudgetError("Semantic summary input exceeded its bounded budget")
    return payload


def _bounded_summary_sources(sources: Sequence[ContextSource]) -> list[ContextSource]:
    selected: list[ContextSource] = []
    index = 0
    while index < len(sources):
        group = [sources[index]]
        message = sources[index].message
        index += 1
        if message.role == "assistant" and message.tool_calls:
            call_ids = {call.id for call in message.tool_calls}
            while (
                index < len(sources)
                and sources[index].message.role == "tool"
                and sources[index].message.tool_call_id in call_ids
            ):
                group.append(sources[index])
                index += 1
        trial = [source.message for source in (*selected, *group)]
        try:
            _summary_input(trial)
        except ContextBudgetError:
            if selected:
                break
            raise
        selected.extend(group)
    return selected


def _parse_summary(
    content: str, sanitizer: Callable[[str], str] | None = None
) -> dict[str, object]:
    if not isinstance(content, str) or "```" in content:
        raise ValueError("semantic summary must be plain JSON")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("semantic summary must be an object")
    if set(value) - set(SUMMARY_FIELDS):
        raise ValueError("semantic summary contains unknown fields")
    normalized: dict[str, object] = {}
    for key in SUMMARY_FIELDS:
        item = value.get(key, [] if key != "objective" else "")
        if key == "objective":
            if not isinstance(item, str) or len(item) > MAX_SUMMARY_TEXT_CHARS:
                raise ValueError("summary objective is invalid")
            normalized[key] = _safe_summary_text(item, sanitizer)
        elif key == "observations":
            if not isinstance(item, list) or len(item) > MAX_SUMMARY_ITEMS:
                raise ValueError("summary observations are invalid")
            observations = []
            for entry in item:
                if not isinstance(entry, dict) or set(entry) - {"tool", "path", "status", "hash"}:
                    raise ValueError("summary observation is invalid")
                clean = {
                    field: entry.get(field, "")
                    for field in ("tool", "path", "status", "hash")
                }
                if any(
                    not isinstance(v, str) or len(v) > MAX_SUMMARY_TEXT_CHARS
                    for v in clean.values()
                ):
                    raise ValueError("summary observation field is invalid")
                clean = {
                    field: _safe_summary_text(value, sanitizer)
                    for field, value in clean.items()
                }
                if clean["path"]:
                    _validate_relative_path(clean["path"])
                observations.append(clean)
            normalized[key] = observations
        else:
            if not isinstance(item, list) or len(item) > MAX_SUMMARY_ITEMS:
                raise ValueError("summary list is invalid")
            cleaned = [_safe_summary_text(entry, sanitizer) for entry in item]
            if key == "changed_files":
                for path in cleaned:
                    _validate_relative_path(path)
            normalized[key] = cleaned
    return normalized


_SECRET_TEXT = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer\s+|sk-[a-z0-9_-]+|<think|<thinking)"
)


def _safe_summary_text(
    value: object, sanitizer: Callable[[str], str] | None = None
) -> str:
    if not isinstance(value, str) or len(value) > MAX_SUMMARY_TEXT_CHARS:
        raise ValueError("summary text is invalid")
    safe = sanitizer(value) if sanitizer is not None else value
    if len(safe) > MAX_SUMMARY_TEXT_CHARS or "```" in safe or _SECRET_TEXT.search(safe):
        raise ValueError("summary text contains sensitive content")
    return safe


def _validate_relative_path(value: str) -> None:
    normalized = value.replace("\\", "/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in normalized.split("/")
    ):
        raise ValueError("summary path must be workspace-relative")


def _count_steps(messages: Sequence[ModelMessage]) -> int:
    return sum(1 for message in messages if message.role == "assistant" and message.tool_calls)


def _validate_tool_protocol(messages: Sequence[ModelMessage]) -> None:
    """Ensure every projected assistant tool call has exactly one result."""

    pending: dict[str, str] = {}
    completed: set[str] = set()
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls:
                if not call.id or call.id in pending or call.id in completed:
                    raise ContextProtocolError(
                        "Projected context contains a duplicate tool call ID"
                    )
                pending[call.id] = call.name
        elif message.role == "tool":
            call_id = message.tool_call_id
            if not call_id or call_id not in pending or call_id in completed:
                raise ContextProtocolError(
                    "Projected context contains an orphan or duplicate tool result"
                )
            completed.add(call_id)
            del pending[call_id]
    if pending:
        raise ContextProtocolError(
            "Projected context contains an assistant tool call without a result"
        )


def _message_size(messages: Sequence[ModelMessage]) -> int:
    size = 0
    for message in messages:
        size += len(message.content) + 64
        for call in message.tool_calls:
            size += len(call.id) + len(call.name) + len(
                json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
            )
    return size


def _messages_digest(messages: Sequence[ModelMessage]) -> str:
    payload = [
        {
            "role": message.role,
            "content": message.content,
            "images": [
                {
                    "media_type": image.media_type,
                    "sha256": hashlib.sha256(image.data).hexdigest(),
                    "byte_size": len(image.data),
                }
                for image in message.images
            ],
            "tool_calls": [call.to_dict() for call in message.tool_calls],
            "tool_call_id": message.tool_call_id,
        }
        for message in messages
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _sources_digest(sources: Sequence[ContextSource]) -> str:
    return hashlib.sha256(
        json.dumps(
            [(source.source_id, _messages_digest((source.message,))) for source in sources],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
