"""LeanHarness command-line entry point."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from leanharness import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanharness",
        description="Local-first coding agent runtime (foundation milestone).",
    )
    parser.add_argument("--version", action="version", version=f"LeanHarness {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
