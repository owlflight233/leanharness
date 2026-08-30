"""LeanHarness command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from leanharness import __version__
from leanharness.application.agent_gateway import create_coding_run
from leanharness.application.model_gateway import check_model
from leanharness.application.plan_gateway import create_plan_generator
from leanharness.application.session_gateway import (
    apply_first_task_title,
    ensure_session,
    history_for_session,
    persist_runtime_event,
)
from leanharness.cli.doctor import collect_diagnostics
from leanharness.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    AppConfig,
    build_config,
    resolve_workspace,
)
from leanharness.errors import LeanHarnessError, ModelError, ModelNotConfiguredError
from leanharness.logging import configure_logging
from leanharness.models import OpenAICompatibleClient, load_model_config
from leanharness.permissions import ApprovalCoordinator, PermissionMode
from leanharness.planning import PlanController, PlanState
from leanharness.runtime.loop import DEFAULT_MAX_STEPS, MAX_MAX_STEPS, MIN_MAX_STEPS
from leanharness.storage import LocalStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanharness",
        description="Local-first coding agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"LeanHarness {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check local development prerequisites without changing the workspace.",
    )
    doctor_parser.add_argument(
        "--workspace",
        help="Workspace directory; defaults to the current directory.",
    )
    doctor_parser.add_argument("--data-dir", help="Local application data directory.")

    serve_parser = subparsers.add_parser("serve", help="Start the local LeanHarness web server.")
    serve_parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Bind host (default: 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        type=int,
        help="Bind port (default: 4318).",
    )
    serve_parser.add_argument(
        "--workspace",
        help="Workspace directory; defaults to the current directory.",
    )
    serve_parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Structured log threshold.",
    )
    serve_parser.add_argument("--data-dir", help="Local application data directory.")

    model_parser = subparsers.add_parser("model", help="Inspect the configured model gateway.")
    model_subparsers = model_parser.add_subparsers(dest="model_command", required=True)
    model_subparsers.add_parser("check", help="Send a small model connectivity check.")

    run_parser = subparsers.add_parser(
        "run", help="Analyze or modify a workspace with the controlled coding agent."
    )
    run_parser.add_argument("task", help="Coding task (1 to 32000 characters).")
    run_parser.add_argument(
        "--workspace",
        help="Workspace directory; defaults to the current directory.",
    )
    run_parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        choices=range(MIN_MAX_STEPS, MAX_MAX_STEPS + 1),
        help=(
            f"Model request budget ({MIN_MAX_STEPS}-{MAX_MAX_STEPS}, default: {DEFAULT_MAX_STEPS})."
        ),
    )
    run_parser.add_argument("--session", dest="session_id", help="Existing session ID.")
    run_parser.add_argument("--data-dir", help="Local application data directory.")
    run_parser.add_argument(
        "--permission",
        choices=("inspect", "approve", "unrestricted"),
        help="Permission for this run; defaults to the session preference.",
    )

    session_parser = subparsers.add_parser("session", help="Manage local persistent sessions.")
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)
    list_parser = session_subparsers.add_parser("list", help="List sessions for a workspace.")
    list_parser.add_argument("--workspace")
    list_parser.add_argument("--data-dir")
    new_parser = session_subparsers.add_parser("new", help="Create a session.")
    new_parser.add_argument("--workspace")
    new_parser.add_argument("--data-dir")
    new_parser.add_argument("--title", default="新会话")
    new_parser.add_argument(
        "--permission", default="inspect", choices=("inspect", "approve", "unrestricted")
    )
    rename_parser = session_subparsers.add_parser("rename", help="Rename a session.")
    rename_parser.add_argument("session_id")
    rename_parser.add_argument("title")
    rename_parser.add_argument("--data-dir")
    delete_parser = session_subparsers.add_parser("delete", help="Delete a session.")
    delete_parser.add_argument("session_id")
    delete_parser.add_argument("--data-dir")

    plan_parser = subparsers.add_parser("plan", help="Generate and manage persistent plans.")
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)
    generate_parser = plan_subparsers.add_parser("generate", help="Generate a draft plan.")
    generate_parser.add_argument("task")
    generate_parser.add_argument("--workspace")
    generate_parser.add_argument("--session", dest="session_id")
    generate_parser.add_argument("--data-dir")
    # `leanharness plan TASK` is the concise form documented for plan generation.
    for command in ("show", "confirm", "reject", "resume", "cancel"):
        command_parser = plan_subparsers.add_parser(command)
        command_parser.add_argument("plan_id")
        command_parser.add_argument("--workspace")
        command_parser.add_argument("--data-dir")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    normalized_argv = list(argv) if argv is not None else sys.argv[1:]
    if len(normalized_argv) >= 2 and normalized_argv[0] == "plan" and normalized_argv[1] not in {
        "generate", "show", "confirm", "reject", "resume", "cancel"
    }:
        normalized_argv.insert(1, "generate")
    args = parser.parse_args(normalized_argv)

    try:
        if args.command == "doctor":
            workspace = resolve_workspace(args.workspace)
            checks = (
                collect_diagnostics(workspace, data_dir=args.data_dir)
                if args.data_dir
                else collect_diagnostics(workspace)
            )
            for check in checks:
                marker = "PASS" if check.ok else "FAIL"
                print(f"[{marker}] {check.name}: {check.detail}")
            return 0 if all(check.ok for check in checks) else 1

        if args.command == "serve":
            config = build_config(
                workspace=args.workspace,
                host=args.host,
                port=args.port,
                log_level=args.log_level,
                data_dir=args.data_dir,
            )
            return _serve(config)

        if args.command == "model" and args.model_command == "check":
            return asyncio.run(_check_model())

        if args.command == "run":
            workspace = resolve_workspace(args.workspace)
            return asyncio.run(
                _inspect(
                    args.task,
                    workspace,
                    args.max_steps,
                    args.session_id,
                    args.data_dir,
                    args.permission,
                )
            )
        if args.command == "session":
            return _session_command(args)
        if args.command == "plan":
            if args.plan_command == "generate":
                return asyncio.run(
                    _plan_generate(
                        args.task,
                        resolve_workspace(args.workspace),
                        args.session_id,
                        args.data_dir,
                    )
                )
            return asyncio.run(
                _plan_lifecycle(
                    args.plan_command,
                    args.plan_id,
                    resolve_workspace(args.workspace),
                    args.data_dir,
                )
            )
    except LeanHarnessError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Run cancelled", file=sys.stderr)
        return 130

    parser.print_help()
    return 0


def _configure_stdio() -> None:
    """Keep multilingual CLI progress readable on Windows code-page terminals."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Test capture streams and already-closed streams may reject this.
            continue


def _serve(config: AppConfig) -> int:
    import uvicorn

    from leanharness.web.app import create_app

    configure_logging(config.log_level)
    print("LeanHarness server started")
    print(f"Workspace: {config.workspace}")
    print(f"Web UI: http://{config.host}:{config.port}")
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_config=None,
    )
    return 0


async def _check_model() -> int:
    try:
        result = await check_model()
    except ModelNotConfiguredError:
        raise
    except ModelError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 3
    print(f"Model check passed: {result.model} ({result.latency_ms} ms)")
    return 0


async def _inspect(
    task: str,
    workspace: Path,
    max_steps: int,
    session_id: str | None,
    data_dir: str | None,
    permission: str | None = None,
) -> int:
    store = LocalStore(Path(data_dir).expanduser() if data_dir else None)
    _, session = ensure_session(
        store, workspace, session_id, permission_mode=permission or "inspect"
    )
    session = apply_first_task_title(store, session, task)
    selected_permission = permission or session.permission_mode
    history = history_for_session(store, session)
    run = store.create_run(
        session.id,
        "coding",
        task,
        max_steps,
        permission_mode=selected_permission,
    )
    store.add_message(session.id, "user", task, run_id=run.id)
    print(f"[session] {session.id}", file=sys.stderr)
    print(f"[run] {run.id}", file=sys.stderr)
    approvals = ApprovalCoordinator(
        on_request=lambda request: store.create_approval(
            request.id,
            request.run_id,
            request.tool_call_id,
            request.tool_name,
            {
                "summary": request.summary,
                "parameters": request.parameters,
                "preview": request.preview,
            },
        ),
        on_resolve=lambda request, decision: store.resolve_approval(request.id, decision),
        on_expire=lambda request: store.expire_approval(request.id),
    )
    runtime = create_coding_run(
        task,
        workspace,
        max_steps=max_steps,
        run_id=run.id,
        language=session.language or "same",
        permission_mode=selected_permission,
        session_id=session.id,
        approvals=approvals,
        history=history,
    )
    exit_code = 0
    async for event in runtime.run(task):
        persist_runtime_event(store, session, run, event)
        if event.type == "assistant.progress":
            print(f"[step {event.step}] {event.summary}", file=sys.stderr)
        elif event.type == "tool.requested":
            print(f"[tool] {event.tool}", file=sys.stderr)
        elif event.type == "approval.required" and event.metadata:
            print(f"[approval] {event.summary or event.tool}", file=sys.stderr)
            parameters = event.metadata.get("parameters")
            if parameters:
                print(f"[preview] {parameters}", file=sys.stderr)
            decision = await asyncio.to_thread(input, "Approve this tool call? [y/N] ")
            approvals.resolve(
                run.id,
                str(event.metadata["approval_id"]),
                "approve" if decision.strip().casefold() in {"y", "yes"} else "reject",
            )
        elif event.type == "approval.resolved" and event.metadata:
            print(f"[approval {event.metadata.get('decision')}] {event.tool}", file=sys.stderr)
        elif event.type == "tool.completed":
            status = "ok" if event.metadata and event.metadata.get("ok") else "error"
            print(f"[tool {status}] {event.tool}", file=sys.stderr)
        elif event.type == "usage.reported" and event.usage:
            if (total := event.usage.get("total_tokens")) is not None:
                print(f"[usage] {total} tokens", file=sys.stderr)
        elif event.type == "run.completed" and event.answer:
            print(event.answer)
        elif event.type == "run.incomplete":
            if event.answer:
                print(event.answer)
            print(f"[incomplete] {event.summary}", file=sys.stderr)
            exit_code = 4
        elif event.type == "run.failed":
            code = event.error_code or "RUN_FAILED"
            print(f"error [{code}]: {event.error_message or 'Run failed'}", file=sys.stderr)
            exit_code = 3 if code.startswith("MODEL_") else 4
        elif event.type == "run.cancelled":
            print("Run cancelled", file=sys.stderr)
            exit_code = 130
    return exit_code


def _session_command(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser() if getattr(args, "data_dir", None) else None
    with LocalStore(data_dir) as store:
        if args.session_command in {"list", "new"}:
            workspace = resolve_workspace(getattr(args, "workspace", None))
            project = store.ensure_project(workspace)
            if args.session_command == "list":
                for session in store.list_sessions(project):
                    print(
                        f"{session.id}\t{session.title}\t{session.permission_mode}\t"
                        f"{session.last_run_state or '-'}"
                    )
                return 0
            session = store.create_session(
                project, title=args.title, permission_mode=args.permission
            )
            print(session.id)
            return 0
        if args.session_command == "rename":
            print(store.update_session(args.session_id, title=args.title).id)
            return 0
        if args.session_command == "delete":
            store.delete_session(args.session_id)
            print(args.session_id)
            return 0
    return 0


async def _plan_generate(
    task: str, workspace: Path, session_id: str | None, data_dir: str | None
) -> int:
    store = LocalStore(Path(data_dir).expanduser() if data_dir else None)
    _, session = ensure_session(store, workspace, session_id)
    session = apply_first_task_title(store, session, task)
    generator = create_plan_generator(workspace, language=session.language or "same")
    generated = None
    try:
        async for item in generator.generate(task):
            if hasattr(item, "markdown"):
                generated = item
    except LeanHarnessError:
        raise
    if generated is None:
        raise LeanHarnessError("The model did not return a valid plan")
    plan = store.create_plan(
        session.id,
        title=generated.title,
        task=task,
        source_markdown=generated.markdown,
        steps=generated.steps,
    )
    print(generated.markdown)
    print(f"[plan] {plan.id} (awaiting confirmation)", file=sys.stderr)
    return 0


async def _plan_lifecycle(
    command: str, plan_id: str, workspace: Path, data_dir: str | None
) -> int:
    store = LocalStore(Path(data_dir).expanduser() if data_dir else None)
    plan = store.get_plan(plan_id)
    if command == "show":
        print(plan.source_markdown)
        print(f"state: {plan.state.value}\nversion: {plan.version}", file=sys.stderr)
        return 0
    if command == "reject":
        store.update_plan_state(plan_id, PlanState.CANCELLED)
        print(f"Plan {plan_id} rejected", file=sys.stderr)
        return 0
    if command == "cancel":
        store.update_plan_state(plan_id, PlanState.CANCELLED)
        print(f"Plan {plan_id} cancelled", file=sys.stderr)
        return 0
    if command not in {"confirm", "resume"}:
        raise LeanHarnessError(f"Unknown plan command: {command}")
    session = store.get_session(plan.session_id)
    if command == "resume" and plan.run_id:
        run = store.get_run(plan.run_id)
    else:
        run = store.create_run(
            session.id, "plan", plan.task, 24, permission_mode=session.permission_mode
        )
        store.attach_plan_run(plan.id, run.id)
    approvals = ApprovalCoordinator()
    controller = PlanController(
        store.get_plan(plan.id),
        workspace=workspace,
        model_client=OpenAICompatibleClient(load_model_config()),
        permission_mode=PermissionMode(session.permission_mode),
        language=session.language or "same",
        approvals=approvals,
    )
    answer = None
    async for event in controller.run():
        if event.type.startswith("run."):
            persist_runtime_event(store, session, run, event)
        else:
            store.append_event(session.id, run.id, event.sequence, event.type, event.to_dict())
        if event.type == "approval.required" and event.metadata:
            decision = await asyncio.to_thread(input, "Approve this plan action? [y/N] ")
            approvals.resolve(
                run.id,
                str(event.metadata.get("approval_id", "")),
                "approve" if decision.strip().casefold() in {"y", "yes"} else "reject",
            )
        if event.type == "run.completed":
            answer = event.answer
        print(f"[{event.type}] {event.summary or ''}", file=sys.stderr)
    if answer:
        print(answer)
    return 0
