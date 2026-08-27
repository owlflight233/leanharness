"""Stable, presentation-safe application errors."""

from __future__ import annotations


class LeanHarnessError(Exception):
    """Base class for expected errors that can be shown to a local user."""

    code = "LEANHARNESS_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(LeanHarnessError):
    """Raised when process configuration is invalid."""

    code = "INVALID_CONFIGURATION"


class WorkspaceError(ConfigurationError):
    """Raised when a workspace cannot be resolved safely."""

    code = "INVALID_WORKSPACE"
