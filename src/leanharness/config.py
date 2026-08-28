"""Application configuration and workspace boundary resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from leanharness.errors import ConfigurationError, WorkspaceError
from leanharness.storage import default_data_dir

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
    data_dir: Path = field(default_factory=default_data_dir)


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


def create_workspace(value: str | Path) -> Path:
    """Create one new workspace directory below an existing parent directory."""

    if isinstance(value, str) and not value.strip():
        raise WorkspaceError("Workspace path must not be blank")
    candidate = Path(value).expanduser()
    if not candidate.name or candidate.name in {".", ".."}:
        raise WorkspaceError("Workspace path must name a new directory")
    if candidate.exists() or candidate.is_symlink():
        raise WorkspaceError(f"Workspace already exists: {candidate}")
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"Workspace parent does not exist: {candidate.parent}") from exc
    if not parent.is_dir():
        raise WorkspaceError(f"Workspace parent is not a directory: {parent}")

    target = parent / candidate.name
    try:
        target.mkdir()
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"Workspace could not be created: {target}") from exc
    if not resolved.is_dir() or resolved.parent != parent:
        target.rmdir()
        raise WorkspaceError("Created workspace could not be resolved safely")
    return resolved


def build_config(
    *,
    workspace: str | Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    log_level: str = DEFAULT_LOG_LEVEL,
    data_dir: str | Path | None = None,
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
        data_dir=(
            Path(data_dir).expanduser().resolve() if data_dir is not None else default_data_dir()
        ),
    )
