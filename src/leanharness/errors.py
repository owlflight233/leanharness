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


class ModelError(LeanHarnessError):
    """Base class for expected model gateway failures."""

    code = "MODEL_ERROR"


class ModelNotConfiguredError(ModelError):
    """Raised when required model settings are missing or invalid."""

    code = "MODEL_NOT_CONFIGURED"


class ModelAuthError(ModelError):
    """Raised when the upstream rejects model credentials."""

    code = "MODEL_AUTH_FAILED"


class ModelRateLimitError(ModelError):
    """Raised when the upstream rate limits a request."""

    code = "MODEL_RATE_LIMITED"


class ModelTimeoutError(ModelError):
    """Raised when the upstream does not respond within the configured timeout."""

    code = "MODEL_TIMEOUT"


class ModelProtocolError(ModelError):
    """Raised when an upstream response does not match the expected protocol."""

    code = "MODEL_PROTOCOL_ERROR"


class ModelUnavailableError(ModelError):
    """Raised when the upstream cannot serve the request."""

    code = "MODEL_UNAVAILABLE"


class ChatInputError(LeanHarnessError):
    """Raised when a single-turn chat message violates the public contract."""

    code = "INVALID_CHAT_INPUT"
