"""Application configuration and workspace boundary resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from leanharness.errors import ConfigurationError, WorkspaceError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4318
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Validated process configuration shared by every interface."""

    workspace: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL


def resolve_workspace(value: str | Path | None = None, *, cwd: Path | None = None) -> Path:
    """Resolve an existing directory to the canonical workspace root."""

    candidate = Path(value).expanduser() if value is not None else (cwd or Path.cwd())
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"Workspace does not exist: {candidate}") from exc

    if not resolved.is_dir():
        raise WorkspaceError(f"Workspace is not a directory: {resolved}")
    return resolved


def build_config(
    *,
    workspace: str | Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    log_level: str = DEFAULT_LOG_LEVEL,
) -> AppConfig:
    """Validate interface inputs and construct immutable application config."""

    normalized_host = host.strip()
    if not normalized_host:
        raise ConfigurationError("Host must not be blank")
    if not 1 <= port <= 65535:
        raise ConfigurationError("Port must be between 1 and 65535")

    normalized_level = log_level.strip().upper()
    if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"Unsupported log level: {log_level}")

    return AppConfig(
        workspace=resolve_workspace(workspace),
        host=normalized_host,
        port=port,
        log_level=normalized_level,
    )
