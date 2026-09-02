"""FastAPI application and same-origin frontend hosting."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from leanharness import __version__
from leanharness.application.agent_gateway import create_coding_run
from leanharness.application.attachments import attachment_to_dict, message_with_attachments
from leanharness.application.health import get_health
from leanharness.application.model_gateway import ModelClientFactory, check_model
from leanharness.application.model_settings import (
    get_effective_model_status,
    load_effective_model_config,
)
from leanharness.application.plan_gateway import create_plan_generator, plan_to_dict
from leanharness.application.plugin_gateway import plugin_registry_factory, plugin_to_dict
from leanharness.application.session_gateway import (
    apply_first_task_title,
    context_history_for_session,
    ensure_session,
    persist_runtime_event,
    session_detail,
    session_to_dict,
)
from leanharness.config import AppConfig, create_workspace, resolve_workspace
from leanharness.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    AttachmentError,
    AttachmentNotFoundError,
    InvalidPermissionError,
    LeanHarnessError,
    ModelNotConfiguredError,
    PlanConflictError,
    PlanNotFoundError,
    PlanStateError,
    PluginError,
    PluginNotFoundError,
    RunConflictError,
    RunInputError,
    SessionNotFoundError,
    StorageError,
    WorkspaceError,
)
from leanharness.models import OpenAICompatibleClient
from leanharness.permissions import (
    ActiveRunRegistry,
    ApprovalCoordinator,
    ApprovalRequest,
    PermissionMode,
)
from leanharness.planning import Plan, PlanController, PlanState, PlanStep, render_plan_markdown
from leanharness.planning.generator import GeneratedPlan, plan_generation_task
from leanharness.plugins.manager import PluginManager
from leanharness.runtime import RuntimeEvent, UserInputCoordinator, UserInputProtocolError
from leanharness.runtime.metrics import RunMetrics
from leanharness.runtime.user_input import UserInputExpiredError
from leanharness.storage import LocalStore


class RunRequest(BaseModel):
    task: str
    max_steps: int = 24
    session_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)
    plugin_ids: list[str] = Field(default_factory=list, max_length=8)
    delegation_enabled: bool = False


class PlanCreateRequest(BaseModel):
    task: str
    session_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)


class PluginInstallRequest(BaseModel):
    path: str


class PluginSelectionRequest(BaseModel):
    plugin_ids: list[str] = Field(default_factory=list, max_length=8)


class PlanStepUpdate(BaseModel):
    id: str
    title: str
    instruction: str
    enabled: bool = True


class PlanUpdateRequest(BaseModel):
    title: str
    steps: list[PlanStepUpdate]
    version: int


class SessionCreateRequest(BaseModel):
    title: str | None = None
    permission_mode: str = "inspect"


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    permission_mode: str | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class UserInputAnswerRequest(BaseModel):
    answer: str


class WorkspaceSelectRequest(BaseModel):
    path: str


class WorkspaceCreateRequest(BaseModel):
    path: str


def default_frontend_dir() -> Path:
    """Locate the repository frontend build when running from a source checkout."""

    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def create_app(
    config: AppConfig,
    *,
    frontend_dir: Path | None = None,
    model_client_factory: ModelClientFactory = OpenAICompatibleClient,
) -> FastAPI:
    """Create the local API without broad cross-origin access."""

    store = LocalStore(config.data_dir)
    store.open()
    store.interrupt_active_runs()

    def serialize_plan(plan: Plan) -> dict[str, object]:
        """Expose the permission that will actually govern plan execution."""
        session = store.get_session(plan.session_id)
        permission = session.permission_mode
        if plan.run_id:
            permission = store.get_run(plan.run_id).permission_mode
        return plan_to_dict(plan, execution_permission_mode=permission)

    def persist_approval_request(request: ApprovalRequest) -> None:
        store.create_approval(
            request.id,
            request.run_id,
            request.tool_call_id,
            request.tool_name,
            {
                "summary": request.summary,
                "parameters": request.parameters,
                "preview": request.preview,
            },
        )

    approvals = ApprovalCoordinator(
        on_request=persist_approval_request,
        on_resolve=lambda request, decision: store.resolve_approval(request.id, decision),
        on_expire=lambda request: store.expire_approval(request.id),
    )
    user_inputs = UserInputCoordinator()
    active_runs = ActiveRunRegistry()
    plugins = PluginManager(store)
    plan_cancellations: dict[str, asyncio.Event] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="LeanHarness API",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.workspace = config.workspace
    app.state.frontend_dir = (frontend_dir or default_frontend_dir()).resolve()
    app.state.model_client_factory = model_client_factory
    app.state.store = store
    app.state.approvals = approvals
    app.state.user_inputs = user_inputs
    app.state.active_runs = active_runs
    app.state.plugins = plugins

    @app.exception_handler(LeanHarnessError)
    async def application_error(_request: Request, exc: LeanHarnessError) -> JSONResponse:
        status_code = 503 if isinstance(exc, ModelNotConfiguredError) else 502
        if isinstance(exc, RunInputError):
            status_code = 422
        elif isinstance(exc, SessionNotFoundError):
            status_code = 404
        elif isinstance(exc, AttachmentError):
            status_code = 404 if exc.code == AttachmentNotFoundError.code else 422
        elif isinstance(exc, PluginNotFoundError):
            status_code = 404
        elif isinstance(exc, (PluginError, InvalidPermissionError)):
            status_code = 422
        elif isinstance(exc, StorageError):
            status_code = 500
        elif isinstance(exc, RunConflictError | ApprovalAlreadyResolvedError):
            status_code = 409
        elif isinstance(exc, ApprovalExpiredError):
            status_code = 410
        elif isinstance(exc, (ApprovalNotFoundError, PlanNotFoundError)):
            status_code = 404
        elif isinstance(exc, (PlanConflictError, PlanStateError)):
            status_code = 409
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_RUN_INPUT",
                    "message": "Request body must contain a task",
                }
            },
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        current = AppConfig(
            workspace=app.state.workspace,
            host=config.host,
            port=config.port,
            log_level=config.log_level,
            data_dir=config.data_dir,
        )
        return get_health(current).to_dict()

    @app.post("/api/v1/workspace")
    async def select_workspace(payload: WorkspaceSelectRequest) -> dict[str, object]:
        if not payload.path.strip():
            raise WorkspaceError("Workspace path must not be blank")
        try:
            selected = resolve_workspace(payload.path)
        except WorkspaceError:
            raise
        if getattr(active_runs, "has_active", lambda: False)():
            raise RunConflictError("Stop the active run before changing workspace")
        app.state.workspace = selected
        return {"workspace": str(selected)}

    @app.post("/api/v1/workspace/create")
    async def create_workspace_route(payload: WorkspaceCreateRequest) -> dict[str, object]:
        if getattr(active_runs, "has_active", lambda: False)():
            raise RunConflictError("Stop the active run before changing workspace")
        selected = create_workspace(payload.path)
        app.state.workspace = selected
        return {"workspace": str(selected), "created": True}

    @app.get("/api/v1/model/status")
    async def model_status() -> dict[str, object]:
        status = get_effective_model_status(app.state.config.data_dir)
        return {
            "configured": status.configured,
            "protocol": status.protocol,
            "model": status.model,
        }

    @app.get("/api/v1/sessions")
    async def sessions() -> dict[str, object]:
        store: LocalStore = app.state.store
        project = store.ensure_project(app.state.workspace)
        return {"sessions": [session_to_dict(session) for session in store.list_sessions(project)]}

    @app.get("/api/v1/projects")
    async def projects() -> dict[str, object]:
        store: LocalStore = app.state.store
        store.ensure_project(app.state.workspace)
        current = str(app.state.workspace.resolve())
        return {
            "current_workspace": current,
            "projects": [
                {
                    "id": project.id,
                    "root_path": project.root_path,
                    "permission_mode": project.permission_mode,
                    "created_at": project.created_at,
                    "updated_at": project.updated_at,
                }
                for project in store.list_projects()
            ],
        }

    @app.post("/api/v1/sessions")
    async def create_session(payload: SessionCreateRequest) -> dict[str, object]:
        store: LocalStore = app.state.store
        project = store.ensure_project(app.state.workspace, permission_mode=payload.permission_mode)
        session = store.create_session(
            project,
            title=payload.title or "新会话",
            permission_mode=payload.permission_mode,
        )
        return session_to_dict(session)

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        ensure_session(app.state.store, app.state.workspace, session_id)
        return session_detail(app.state.store, session_id)

    @app.patch("/api/v1/sessions/{session_id}")
    async def update_session(session_id: str, payload: SessionUpdateRequest) -> dict[str, object]:
        ensure_session(app.state.store, app.state.workspace, session_id)
        if payload.permission_mode is not None:
            active_runs.assert_available(session_id)
        session = app.state.store.update_session(
            session_id,
            title=payload.title,
            permission_mode=payload.permission_mode,
        )
        return session_to_dict(session)

    @app.delete("/api/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, object]:
        ensure_session(app.state.store, app.state.workspace, session_id)
        app.state.store.delete_session(session_id)
        return {"deleted": True, "session_id": session_id}

    @app.post("/api/v1/model/check")
    async def model_check() -> dict[str, object]:
        result = await check_model(
            data_dir=str(app.state.config.data_dir),
            client_factory=app.state.model_client_factory,
        )
        return result.to_dict()

    @app.get("/api/v1/plugins")
    async def list_plugins() -> dict[str, object]:
        return {"plugins": [plugin_to_dict(plugin) for plugin in plugins.list()]}

    @app.post("/api/v1/plugins/install")
    async def install_plugin(payload: PluginInstallRequest) -> dict[str, object]:
        if not payload.path.strip():
            raise PluginError("Plugin path must not be blank")
        return plugin_to_dict(plugins.install(payload.path))

    @app.post("/api/v1/plugins/{plugin_id}/enable")
    async def enable_plugin(plugin_id: str) -> dict[str, object]:
        return plugin_to_dict(plugins.enable(plugin_id))

    @app.post("/api/v1/plugins/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str) -> dict[str, object]:
        return plugin_to_dict(plugins.disable(plugin_id))

    @app.delete("/api/v1/plugins/{plugin_id}")
    async def remove_plugin(plugin_id: str) -> dict[str, object]:
        plugins.remove(plugin_id)
        return {"deleted": True, "plugin_id": plugin_id}

    @app.post("/api/v1/attachments")
    async def upload_attachment(session_id: str, file: UploadFile = File(...)) -> dict[str, object]:  # noqa: B008
        store: LocalStore = app.state.store
        _, session = ensure_session(store, app.state.workspace, session_id)
        try:
            data = await file.read(20 * 1024 * 1024 + 1)
            if not data:
                raise AttachmentError("Attachment must not be empty")
            attachment = store.create_attachment(
                session.id,
                file.filename or "attachment",
                file.content_type,
                data,
            )
            return attachment_to_dict(attachment)
        finally:
            await file.close()

    @app.get("/api/v1/attachments/{attachment_id}")
    async def get_attachment(attachment_id: str) -> dict[str, object]:
        attachment = app.state.store.get_attachment(attachment_id)
        ensure_session(app.state.store, app.state.workspace, attachment.session_id)
        return attachment_to_dict(attachment)

    @app.delete("/api/v1/attachments/{attachment_id}")
    async def delete_attachment(attachment_id: str) -> dict[str, object]:
        attachment = app.state.store.get_attachment(attachment_id)
        _, session = ensure_session(app.state.store, app.state.workspace, attachment.session_id)
        app.state.store.delete_attachment(attachment_id, session_id=session.id)
        return {"deleted": True, "attachment_id": attachment_id}

    @app.post("/api/v1/runs")
    async def run(payload: RunRequest) -> StreamingResponse:
        store: LocalStore = app.state.store
        _, session = ensure_session(store, app.state.workspace, payload.session_id)
        session = apply_first_task_title(store, session, payload.task)
        active_runs.assert_available(session.id)
        history = context_history_for_session(store, session)
        attachment_ids = tuple(payload.attachment_ids)
        user_message = message_with_attachments(
            store, session.id, payload.task, attachment_ids
        )
        selected_tool_registry = plugin_registry_factory(
            store, app.state.workspace, tuple(payload.plugin_ids)
        )
        run_record = store.create_run(
            session.id,
            "coding",
            payload.task,
            payload.max_steps,
            permission_mode=session.permission_mode,
        )
        active_runs.acquire(session.id, run_record.id)
        runtime = create_coding_run(
            payload.task,
            app.state.workspace,
            max_steps=payload.max_steps,
            client_factory=app.state.model_client_factory,
            run_id=run_record.id,
            language=session.language or "same",
            permission_mode=session.permission_mode,
            session_id=session.id,
            approvals=approvals,
            user_inputs=user_inputs,
            history_sources=history,
            context_sanitizer=store.redactor.text,
            data_dir=app.state.config.data_dir,
            user_message=user_message,
            tool_registry_factory=selected_tool_registry,
            enable_delegation=payload.delegation_enabled,
        )
        store.add_message(
            session.id,
            "user",
            payload.task,
            run_id=run_record.id,
            attachment_ids=attachment_ids,
        )

        async def ndjson_events() -> AsyncIterator[bytes]:
            last_sequence = -1
            terminal_seen = False
            try:
                async for event in runtime.run(payload.task):
                    last_sequence = event.sequence
                    persist_runtime_event(store, session, run_record, event)
                    terminal_seen = event.type in {
                        "run.completed",
                        "run.incomplete",
                        "run.failed",
                        "run.cancelled",
                    }
                    yield _encode_payload(
                        event.to_dict(), session_id=session.id, run_id=run_record.id
                    )
            except (asyncio.CancelledError, GeneratorExit):
                if not terminal_seen:
                    cancellation = RuntimeEvent(
                        type="run.cancelled",
                        sequence=last_sequence + 1,
                        run_id=run_record.id,
                        summary="Run cancelled",
                    )
                    persist_runtime_event(store, session, run_record, cancellation)
                raise
            finally:
                approvals.cancel_run(run_record.id)
                user_inputs.cancel_run(run_record.id)
                active_runs.release(session.id, run_record.id)

        return StreamingResponse(
            ndjson_events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/v1/plans")
    async def create_plan(payload: PlanCreateRequest) -> dict[str, object]:
        task = payload.task.strip()
        if not task or len(task) > 32_000:
            raise RunInputError("Plan task must be between 1 and 32000 characters")
        store: LocalStore = app.state.store
        _, session = ensure_session(store, app.state.workspace, payload.session_id)
        session = apply_first_task_title(store, session, task)
        run = store.create_run(session.id, "plan_draft", task, 8, permission_mode="inspect")
        attachment_ids = tuple(payload.attachment_ids)
        user_message = message_with_attachments(
            store, session.id, plan_generation_task(task), attachment_ids
        )
        store.add_message(
            session.id, "user", task, run_id=run.id, attachment_ids=attachment_ids
        )
        history = context_history_for_session(store, session, exclude_run_id=run.id)
        generator = create_plan_generator(
            app.state.workspace,
            language=session.language or "same",
            run_id=run.id,
            session_id=session.id,
            client_factory=app.state.model_client_factory,
            history_sources=history,
            context_sanitizer=store.redactor.text,
            data_dir=app.state.config.data_dir,
            user_message=user_message,
        )
        generated: GeneratedPlan | None = None
        generation_events: list[RuntimeEvent] = []
        async for item in generator.generate(task):
            if isinstance(item, GeneratedPlan):
                generated = item
            else:
                generation_events.append(item)
                payload = item.to_dict()
                store.append_event(session.id, run.id, item.sequence, item.type, payload)
        if generated is None:
            store.update_run(run.id, state="FAILED", error_code="PLAN_INVALID_FORMAT")
            raise RunInputError("The model did not return a valid plan")
        plan = store.create_plan(
            session.id,
            title=generated.title,
            task=task,
            source_markdown=generated.markdown,
            steps=generated.steps,
        )
        store.add_message(
            session.id,
            "assistant",
            generated.markdown,
            run_id=run.id,
            kind="plan",
            plan_id=plan.id,
        )
        store.update_run(run.id, state="COMPLETED", answer=generated.markdown)
        created_sequence = (generation_events[-1].sequence + 1) if generation_events else 0
        store.append_event(
            session.id,
            run.id,
            created_sequence,
            "plan.created",
            _plan_created_audit_payload(plan, created_sequence, run.id),
        )
        result = serialize_plan(plan)
        result["generation_trace"] = [event.to_dict() for event in generation_events]
        return result

    @app.post("/api/v1/plans/stream")
    async def stream_plan_creation(payload: PlanCreateRequest) -> StreamingResponse:
        task = payload.task.strip()
        if not task or len(task) > 32_000:
            raise RunInputError("Plan task must be between 1 and 32000 characters")
        store: LocalStore = app.state.store
        _, session = ensure_session(store, app.state.workspace, payload.session_id)
        session = apply_first_task_title(store, session, task)
        active_runs.assert_available(session.id)
        run = store.create_run(session.id, "plan_draft", task, 8, permission_mode="inspect")
        attachment_ids = tuple(payload.attachment_ids)
        user_message = message_with_attachments(
            store, session.id, plan_generation_task(task), attachment_ids
        )
        store.add_message(
            session.id, "user", task, run_id=run.id, attachment_ids=attachment_ids
        )
        history = context_history_for_session(store, session, exclude_run_id=run.id)
        generator = create_plan_generator(
            app.state.workspace,
            language=session.language or "same",
            run_id=run.id,
            session_id=session.id,
            client_factory=app.state.model_client_factory,
            history_sources=history,
            context_sanitizer=store.redactor.text,
            data_dir=app.state.config.data_dir,
            user_message=user_message,
        )
        active_runs.acquire(session.id, run.id)

        async def plan_events() -> AsyncIterator[bytes]:
            generated: GeneratedPlan | None = None
            last_sequence = -1
            terminal_seen = False
            try:
                async for item in generator.generate(task):
                    if isinstance(item, GeneratedPlan):
                        generated = item
                        continue
                    last_sequence = item.sequence
                    terminal_seen = item.type in {
                        "run.completed",
                        "run.incomplete",
                        "run.failed",
                        "run.cancelled",
                    }
                    event_payload = item.to_dict()
                    store.append_event(
                        session.id, run.id, item.sequence, item.type, event_payload
                    )
                    yield _encode_payload(
                        event_payload, session_id=session.id, run_id=run.id
                    )
                if generated is None:
                    store.update_run(
                        run.id, state="FAILED", error_code="PLAN_INVALID_FORMAT"
                    )
                    yield _encode_payload(
                        {
                            "type": "plan.failed",
                            "sequence": last_sequence + 1,
                            "run_id": run.id,
                            "error": {
                                "code": "PLAN_INVALID_FORMAT",
                                "message": "The model did not return a valid plan",
                            },
                        },
                        session_id=session.id,
                        run_id=run.id,
                    )
                    return
                plan = store.create_plan(
                    session.id,
                    title=generated.title,
                    task=task,
                    source_markdown=generated.markdown,
                    steps=generated.steps,
                )
                store.add_message(
                    session.id,
                    "assistant",
                    generated.markdown,
                    run_id=run.id,
                    kind="plan",
                    plan_id=plan.id,
                )
                store.update_run(run.id, state="COMPLETED", answer=generated.markdown)
                created_sequence = last_sequence + 1
                store.append_event(
                    session.id,
                    run.id,
                    created_sequence,
                    "plan.created",
                    _plan_created_audit_payload(plan, created_sequence, run.id),
                )
                yield _encode_payload(
                    {
                        "type": "plan.created",
                        "sequence": created_sequence,
                        "run_id": run.id,
                        "plan": serialize_plan(plan),
                    },
                    session_id=session.id,
                    run_id=run.id,
                )
            except (asyncio.CancelledError, GeneratorExit):
                if not terminal_seen:
                    store.update_run(run.id, state="CANCELLED")
                raise
            finally:
                active_runs.release(session.id, run.id)

        return StreamingResponse(
            plan_events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/v1/plans/{plan_id}")
    async def get_plan(plan_id: str) -> dict[str, object]:
        plan = app.state.store.get_plan(plan_id)
        session = app.state.store.get_session(plan.session_id)
        if session.project_id != app.state.store.ensure_project(app.state.workspace).id:
            raise SessionNotFoundError("Plan does not belong to the current workspace")
        return serialize_plan(plan)

    @app.patch("/api/v1/plans/{plan_id}")
    async def update_plan(plan_id: str, payload: PlanUpdateRequest) -> dict[str, object]:
        current = app.state.store.get_plan(plan_id)
        if not payload.title.strip() or not 1 <= len(payload.steps) <= 32:
            raise RunInputError("A plan requires 1 to 32 non-empty steps")
        if any(
            not step.title.strip()
            or not step.instruction.strip()
            or len(step.instruction) > 2_000
            for step in payload.steps
        ):
            raise RunInputError("Plan step title and instruction are required and bounded")
        steps = tuple(
            PlanStep(
                id=step.id,
                sequence=index + 1,
                title=step.title,
                instruction=step.instruction,
                enabled=step.enabled,
            )
            for index, step in enumerate(payload.steps)
        )
        updated = app.state.store.update_plan(
            plan_id,
            version=payload.version,
            title=payload.title,
            source_markdown=render_plan_markdown(current, steps),
            steps=steps,
        )
        return serialize_plan(updated)

    @app.post("/api/v1/plans/{plan_id}/reject")
    async def reject_plan(plan_id: str) -> dict[str, object]:
        plan = app.state.store.update_plan_state(plan_id, PlanState.CANCELLED)
        return serialize_plan(plan)

    async def execute_plan(
        plan_id: str,
        *,
        resume: bool = False,
        plugin_ids: tuple[str, ...] = (),
    ) -> StreamingResponse:
        store: LocalStore = app.state.store
        plan = store.get_plan(plan_id)
        if resume:
            if plan.state is not PlanState.PAUSED:
                raise LeanHarnessError("Only a paused plan can be resumed")
        elif plan.state is not PlanState.AWAITING_CONFIRMATION:
            raise LeanHarnessError("Only an unconfirmed plan can be confirmed")
        session = store.get_session(plan.session_id)
        active_runs.assert_available(session.id)
        selected_tool_registry = plugin_registry_factory(
            store, app.state.workspace, plugin_ids
        )
        model = app.state.model_client_factory(
            load_effective_model_config(app.state.config.data_dir)
        )
        if resume and plan.run_id:
            run = store.get_run(plan.run_id)
            previous_permission = run.permission_mode
            run = store.resume_run(run.id, permission_mode=session.permission_mode)
            store.update_plan_state(plan.id, PlanState.RUNNING)
        else:
            previous_permission = None
            run = store.create_run(
                session.id, "plan", plan.task, 24, permission_mode=session.permission_mode
            )
            store.attach_plan_run(plan.id, run.id)
        plan = store.get_plan(plan.id)
        active_runs.acquire(session.id, run.id)
        existing_events = store.list_events(run.id)
        initial_sequence = (
            int(existing_events[-1].get("sequence", -1)) + 1 if existing_events else 0
        )
        permission_event: dict[str, object] | None = None
        if resume and previous_permission != run.permission_mode:
            permission_event = {
                "type": "run.permission.updated",
                "sequence": initial_sequence,
                "run_id": run.id,
                "summary": "Execution permission updated for resumed plan",
                "metadata": {
                    "previous_permission_mode": previous_permission,
                    "permission_mode": run.permission_mode,
                },
            }
            store.append_event(
                session.id,
                run.id,
                initial_sequence,
                "run.permission.updated",
                permission_event,
            )
            initial_sequence += 1
        controller = PlanController(
            plan,
            app.state.workspace,
            model,
            max_steps=None,
            permission_mode=PermissionMode(run.permission_mode),
            language=session.language or "same",
            approvals=approvals,
            on_step=lambda step_id, state, evidence, error: store.update_plan_step(
                step_id, state, evidence=evidence, error_code=error
            ),
            initial_sequence=initial_sequence,
            cancel_event=plan_cancellations.setdefault(run.id, asyncio.Event()),
            history_sources=context_history_for_session(
                store, session, exclude_run_id=run.id
            ),
            initial_metrics=RunMetrics.from_events(existing_events) if resume else None,
            tool_registry_factory=selected_tool_registry,
        )

        async def events() -> AsyncIterator[bytes]:
            terminal = False
            try:
                if permission_event is not None:
                    yield _encode_payload(
                        permission_event, session_id=session.id, run_id=run.id
                    )
                async for event in controller.run():
                    payload = event.to_dict()
                    store.append_event(
                        session.id, run.id, int(payload["sequence"]), event.type, payload
                    )
                    if event.type == "plan.completed":
                        store.update_plan_state(plan.id, PlanState.COMPLETED)
                    elif event.type == "plan.paused":
                        store.update_plan_state(plan.id, PlanState.PAUSED)
                    elif event.type == "plan.failed":
                        store.update_plan_state(
                            plan.id, PlanState.FAILED, error_code=event.error_code
                        )
                    elif event.type == "plan.cancelled":
                        store.update_plan_state(plan.id, PlanState.CANCELLED)
                    if event.type in {
                        "run.completed",
                        "run.incomplete",
                        "run.failed",
                        "run.cancelled",
                    }:
                        terminal = True
                        answer = getattr(event, "answer", None)
                        if answer:
                            store.add_message(
                                session.id,
                                "assistant",
                                answer,
                                {
                                    "run.completed": "complete",
                                    "run.incomplete": "incomplete",
                                    "run.failed": "error",
                                    "run.cancelled": "cancelled",
                                }[event.type],
                                run_id=run.id,
                            )
                        store.update_run(
                            run.id,
                            state={
                                "run.completed": "COMPLETED",
                                "run.incomplete": "EXHAUSTED",
                                "run.failed": "FAILED",
                                "run.cancelled": "CANCELLED",
                            }[event.type],
                            answer=answer,
                            error_code=getattr(event, "error_code", None),
                        )
                    yield _encode_payload(payload, session_id=session.id, run_id=run.id)
            except asyncio.CancelledError:
                if not terminal:
                    store.update_plan_state(plan.id, PlanState.PAUSED)
                raise
            finally:
                approvals.cancel_run(run.id)
                active_runs.release(session.id, run.id)
                plan_cancellations.pop(run.id, None)

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/api/v1/plans/{plan_id}/confirm")
    async def confirm_plan(
        plan_id: str, payload: PluginSelectionRequest | None = None
    ) -> StreamingResponse:
        return await execute_plan(
            plan_id, plugin_ids=tuple(payload.plugin_ids) if payload else ()
        )

    @app.post("/api/v1/plans/{plan_id}/resume")
    async def resume_plan(
        plan_id: str, payload: PluginSelectionRequest | None = None
    ) -> StreamingResponse:
        return await execute_plan(
            plan_id,
            resume=True,
            plugin_ids=tuple(payload.plugin_ids) if payload else (),
        )

    @app.post("/api/v1/plans/{plan_id}/cancel")
    async def cancel_plan(plan_id: str) -> dict[str, object]:
        plan = app.state.store.get_plan(plan_id)
        if plan.run_id and plan.run_id in plan_cancellations:
            plan_cancellations[plan.run_id].set()
        plan = app.state.store.update_plan_state(plan_id, PlanState.CANCELLED)
        return serialize_plan(plan)

    @app.post("/api/v1/runs/{run_id}/approvals/{approval_id}")
    async def resolve_approval(
        run_id: str, approval_id: str, payload: ApprovalDecisionRequest
    ) -> dict[str, object]:
        request = approvals.resolve(run_id, approval_id, payload.decision)
        return {
            "approval_id": request.id,
            "run_id": request.run_id,
            "decision": payload.decision,
            "status": "resolved",
        }

    @app.post("/api/v1/runs/{run_id}/questions/{input_id}")
    async def resolve_user_input(
        run_id: str, input_id: str, payload: UserInputAnswerRequest
    ) -> dict[str, object]:
        try:
            request = user_inputs.resolve(run_id, input_id, payload.answer)
        except UserInputExpiredError as exc:
            raise HTTPException(
                status_code=410,
                detail={"code": "INPUT_EXPIRED", "message": str(exc)},
            ) from exc
        except UserInputProtocolError as exc:
            status_code = 422 if exc.code == "INPUT_INVALID_ANSWER" else 404
            if exc.code == "INPUT_ALREADY_RESOLVED":
                status_code = 409
            raise HTTPException(
                status_code=status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        return {
            "input_id": request.id,
            "run_id": request.run_id,
            "status": "resolved",
        }

    @app.get("/", include_in_schema=False)
    @app.get("/{requested_path:path}", include_in_schema=False)
    async def frontend(requested_path: str = "") -> Response:
        if requested_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")

        root: Path = app.state.frontend_dir
        index = root / "index.html"
        requested = (root / requested_path).resolve()
        if requested.is_relative_to(root) and requested.is_file():
            return FileResponse(requested)
        if index.is_file() and not Path(requested_path).suffix:
            return FileResponse(index)
        if not index.is_file():
            return JSONResponse(
                status_code=503,
                content={
                    "status": "frontend-unavailable",
                    "detail": "Build the React client with `pnpm build` in frontend/.",
                },
            )
        raise HTTPException(status_code=404, detail="Static asset not found")

    return app


def _encode_payload(
    payload: dict[str, object], *, session_id: str | None = None, run_id: str | None = None
) -> bytes:
    if session_id is not None:
        payload = {**payload, "session_id": session_id}
    if run_id is not None:
        payload = {**payload, "run_id": run_id}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _plan_created_audit_payload(plan: object, sequence: int, run_id: str) -> dict[str, object]:
    """Persist lifecycle metadata without storing full plan text or instructions."""
    plan_id = getattr(plan, "id", "")
    title = getattr(plan, "title", "")
    state_value = getattr(plan, "state", "")
    state = getattr(state_value, "value", state_value)
    steps = getattr(plan, "steps", ())
    return {
        "type": "plan.created",
        "sequence": sequence,
        "run_id": run_id,
        "plan_id": str(plan_id),
        "title": str(title),
        "state": str(state),
        "step_count": len(steps),
    }
