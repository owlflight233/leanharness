"""LeanHarness command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from leanharness import __version__
from leanharness.application.model_gateway import check_model, stream_chat
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
from leanharness.models import ModelConfig, load_model_config


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

    model_parser = subparsers.add_parser("model", help="Inspect the configured model gateway.")
    model_subparsers = model_parser.add_subparsers(dest="model_command", required=True)
    model_subparsers.add_parser("check", help="Send a small model connectivity check.")

    chat_parser = subparsers.add_parser("chat", help="Run one ephemeral streaming model turn.")
    chat_parser.add_argument("message", help="User message (1 to 32000 characters).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            workspace = resolve_workspace(args.workspace)
            checks = collect_diagnostics(workspace)
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
            )
            return _serve(config)

        if args.command == "model" and args.model_command == "check":
            return asyncio.run(_check_model())

        if args.command == "chat":
            model_config = load_model_config()
            return asyncio.run(_chat(args.message, model_config))
    except LeanHarnessError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2

    parser.print_help()
    return 0


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


async def _chat(message: str, config: ModelConfig) -> int:
    wrote_content = False
    failed = False
    async for event in stream_chat(message, config=config):
        if event.type == "content.delta" and event.content:
            print(event.content, end="", flush=True)
            wrote_content = True
        elif event.type == "usage.reported" and event.usage:
            total = event.usage.total_tokens
            if total is not None:
                print(f"usage: {total} tokens", file=sys.stderr)
        elif event.type == "turn.failed":
            if wrote_content:
                print()
            error_code = event.error_code or "MODEL_ERROR"
            error_message = event.error_message or "Model request failed"
            print(f"error [{error_code}]: {error_message}", file=sys.stderr)
            failed = True
    if wrote_content and not failed:
        print()
    return 3 if failed else 0
