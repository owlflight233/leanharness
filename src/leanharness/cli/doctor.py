"""Read-only local dependency diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from leanharness.storage import LocalStore


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str


CommandProbe = Callable[[str], str | None]


def probe_command(name: str) -> str | None:
    """Return the first version line for a known executable without using a shell."""

    executable = shutil.which(name)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    output = (completed.stdout or completed.stderr).strip().splitlines()
    if completed.returncode != 0 or not output:
        return None
    return output[0]


def collect_diagnostics(
    workspace: Path,
    *,
    command_probe: CommandProbe = probe_command,
    data_dir: str | Path | None = None,
) -> tuple[DiagnosticCheck, ...]:
    """Inspect prerequisites without writing to the selected workspace."""

    checks = [
        DiagnosticCheck(
            name="python",
            ok=sys.version_info >= (3, 12),
            detail=(
                f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
        )
    ]

    for command in ("git", "node", "pnpm"):
        version = command_probe(command)
        checks.append(
            DiagnosticCheck(
                name=command,
                ok=version is not None,
                detail=version or "not found",
            )
        )

    checks.extend(
        [
            DiagnosticCheck(
                name="workspace-readable",
                ok=os.access(workspace, os.R_OK),
                detail=str(workspace),
            ),
            DiagnosticCheck(
                name="workspace-writable",
                ok=os.access(workspace, os.W_OK),
                detail=str(workspace),
            ),
        ]
    )
    target = Path(data_dir).expanduser() if data_dir is not None else None
    try:
        with LocalStore(target) as store:
            store.connection.execute("SELECT 1")
        checks.append(
            DiagnosticCheck("data-storage", True, str(target or "default user data directory"))
        )
    except Exception:
        checks.append(DiagnosticCheck("data-storage", False, "SQLite data directory unavailable"))
    return tuple(checks)
