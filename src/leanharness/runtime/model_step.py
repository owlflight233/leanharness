"""One bounded model-request step with projection and recoverable compaction."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from leanharness.context import ContextBudgetError, ContextProjection, ContextSource, ContextStore
from leanharness.errors import ModelContextLengthError, ModelProtocolError
from leanharness.models import ModelMessage, ModelRequest, ModelResponse
from leanharness.runtime.metrics import RunMetrics
from leanharness.runtime.recovery import ModelProtocolRecovery, ProtocolRepair


class StepModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class ProjectionSignal:
    projection: ContextProjection
    compacted: bool = False


@dataclass(frozen=True, slots=True)
class RequestStartedSignal:
    pass


@dataclass(frozen=True, slots=True)
class ResponseSignal:
    response: ModelResponse


@dataclass(frozen=True, slots=True)
class ProtocolRepairSignal:
    repair: ProtocolRepair


ModelStepSignal = (
    ProjectionSignal | RequestStartedSignal | ResponseSignal | ProtocolRepairSignal
)
RequestBuilder = Callable[[tuple[ModelMessage, ...], bool], ModelRequest]


class ModelStepExecutor:
    """Execute model I/O while leaving Turn state transitions to CodingAgent."""

    def __init__(
        self,
        *,
        context: ContextStore,
        model_client: StepModelClient,
        metrics: RunMetrics,
        protocol_recovery: ModelProtocolRecovery,
        request_builder: RequestBuilder,
        language: str,
    ) -> None:
        self._context = context
        self._model_client = model_client
        self._metrics = metrics
        self._protocol_recovery = protocol_recovery
        self._request_builder = request_builder
        self._language = language

    async def execute(
        self,
        *,
        history_sources: tuple[ContextSource, ...],
        summary_round: bool,
    ) -> AsyncIterator[ModelStepSignal]:
        projection = await self._project(history_sources)
        yield ProjectionSignal(projection)
        if projection.changed or projection.semantic_fallback:
            yield ProjectionSignal(projection, compacted=True)
        yield RequestStartedSignal()
        try:
            response = await self._complete(projection, summary_round)
        except ModelContextLengthError:
            recovered = await self._project(history_sources, force_semantic=True)
            if recovered.digest == projection.digest:
                raise ContextBudgetError(
                    "Context compaction did not produce a smaller request"
                ) from None
            yield ProjectionSignal(recovered, compacted=True)
            try:
                response = await self._complete(recovered, summary_round)
            except ModelContextLengthError as exc:
                raise ContextBudgetError(
                    "Model context window remained exceeded after one recovery"
                ) from exc
        except ModelProtocolError:
            repair = self._protocol_recovery.request(self._language)
            if repair is None:
                raise
            self._context.append(repair.message)
            yield ProtocolRepairSignal(repair)
            return
        yield ResponseSignal(response)

    async def _project(
        self,
        history_sources: tuple[ContextSource, ...],
        *,
        force_semantic: bool = False,
    ) -> ContextProjection:
        projection = await self._context.projector.project_async(
            history_sources,
            self._context,
            self._model_client,
            force_semantic=force_semantic,
        )
        self._metrics.record_projection(
            chars=projection.projected_chars,
            messages=len(projection.messages),
            compressed_steps=projection.compressed_steps,
            compressed_tool_results=projection.compressed_messages,
            semantic_calls=self._context.projector.semantic_calls,
            semantic_fallback=projection.semantic_fallback,
            generation=projection.generation,
        )
        return projection

    async def _complete(
        self,
        projection: ContextProjection,
        summary_round: bool,
    ) -> ModelResponse:
        self._metrics.model_calls += 1
        return await self._model_client.complete(
            self._request_builder(projection.messages, summary_round)
        )
