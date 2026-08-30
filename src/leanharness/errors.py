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


class RunInputError(LeanHarnessError):
    """Raised when an inspection-run request violates the public contract."""

    code = "INVALID_RUN_INPUT"


class StorageError(LeanHarnessError):
    """Raised when local session storage cannot be opened or updated."""

    code = "STORAGE_ERROR"


class SessionNotFoundError(StorageError):
    """Raised when a requested local session does not exist."""

    code = "SESSION_NOT_FOUND"


class InvalidPermissionError(StorageError):
    """Raised when a permission mode is unknown or malformed."""

    code = "INVALID_PERMISSION_MODE"


class RunConflictError(LeanHarnessError):
    """Raised when a session already owns an active coding run."""

    code = "RUN_ALREADY_ACTIVE"


class PlanFormatError(LeanHarnessError):
    """Raised when model output cannot be parsed as the limited plan format."""

    code = "PLAN_INVALID_FORMAT"


class PlanNotFoundError(LeanHarnessError):
    code = "PLAN_NOT_FOUND"


class PlanConflictError(LeanHarnessError):
    code = "PLAN_VERSION_CONFLICT"


class PlanStateError(LeanHarnessError):
    code = "PLAN_STATE_INVALID"


class ApprovalNotFoundError(LeanHarnessError):
    code = "APPROVAL_NOT_FOUND"


class ApprovalAlreadyResolvedError(LeanHarnessError):
    code = "APPROVAL_ALREADY_RESOLVED"


class ApprovalExpiredError(LeanHarnessError):
    code = "APPROVAL_EXPIRED"
