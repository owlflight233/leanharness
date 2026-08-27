"""LeanHarness command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from leanharness import __version__
from leanharness.cli.doctor import collect_diagnostics
from leanharness.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    AppConfig,
    build_config,
    resolve_workspace,
)
from leanharness.errors import LeanHarnessError
from leanharness.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanharness",
        description="Local-first coding agent runtime (foundation milestone).",
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
