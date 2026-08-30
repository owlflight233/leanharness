"""Validated model gateway configuration loaded from the process environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit

from leanharness.errors import ModelNotConfiguredError

MODEL_PROTOCOL = "openai-compatible"
MODEL_BASE_URL_ENV = "LEANHARNESS_MODEL_BASE_URL"
MODEL_NAME_ENV = "LEANHARNESS_MODEL_NAME"
MODEL_API_KEY_ENV = "LEANHARNESS_MODEL_API_KEY"
MODEL_API_KEY_ALIASES = (MODEL_API_KEY_ENV, "DEEPSEEK_API_KEY")
MODEL_THINKING_ENV = "LEANHARNESS_MODEL_THINKING"
MODEL_REASONING_EFFORT_ENV = "LEANHARNESS_MODEL_REASONING_EFFORT"


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Complete model settings; credentials are excluded from representations."""

    base_url: str
    model: str
    api_key: str | None = field(repr=False, default=None)
    thinking: bool = False
    reasoning_effort: str | None = None
    protocol: str = MODEL_PROTOCOL

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"


@dataclass(frozen=True, slots=True)
class ModelConfigStatus:
    """Credential-free model configuration projection for local interfaces."""

    configured: bool
    protocol: str = MODEL_PROTOCOL
    model: str | None = None


def load_model_config(
    environ: Mapping[str, str] | None = None,
    *,
    defaults: Mapping[str, str] | None = None,
) -> ModelConfig:
    """Load and validate model settings without retaining the environment mapping."""

    values = os.environ if environ is None else environ
    fallback = defaults or {}

    def setting(name: str, default: str = "") -> str:
        return values.get(name, "").strip() or fallback.get(name, default).strip()

    base_url = setting(MODEL_BASE_URL_ENV)
    model = setting(MODEL_NAME_ENV)
    api_key = next(
        (
            values.get(name, "").strip()
            for name in MODEL_API_KEY_ALIASES
            if values.get(name, "").strip()
        ),
        None,
    )
    thinking_value = setting(MODEL_THINKING_ENV, "enabled").lower()
    if thinking_value not in {"enabled", "disabled", "true", "false", "1", "0"}:
        raise ModelNotConfiguredError(
            f"{MODEL_THINKING_ENV} must be enabled or disabled"
        )
    thinking = thinking_value in {"enabled", "true", "1"}
    reasoning_effort = setting(MODEL_REASONING_EFFORT_ENV, "high") or None

    missing = [
        name
        for name, value in ((MODEL_BASE_URL_ENV, base_url), (MODEL_NAME_ENV, model))
        if not value
    ]
    if missing:
        raise ModelNotConfiguredError(
            "Model configuration is incomplete; set " + " and ".join(missing)
        )

    return ModelConfig(
        base_url=_normalize_base_url(base_url),
        model=model,
        api_key=api_key,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def get_model_config_status(
    environ: Mapping[str, str] | None = None,
    *,
    defaults: Mapping[str, str] | None = None,
) -> ModelConfigStatus:
    """Return a safe status projection without exposing credentials or endpoint details."""

    values = os.environ if environ is None else environ
    model = (
        values.get(MODEL_NAME_ENV, "").strip()
        or (defaults or {}).get(MODEL_NAME_ENV, "").strip()
        or None
    )
    try:
        config = load_model_config(values, defaults=defaults)
    except ModelNotConfiguredError:
        return ModelConfigStatus(configured=False, model=model)
    return ModelConfigStatus(configured=True, model=config.model)


def _normalize_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ModelNotConfiguredError("Model base URL is invalid") from exc

    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelNotConfiguredError(
            "Model base URL must not contain credentials, a query, or a fragment"
        )
    if not parsed.hostname:
        raise ModelNotConfiguredError("Model base URL must include a host")

    hostname = parsed.hostname.lower()
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and hostname in local_hosts):
        raise ModelNotConfiguredError(
            "Model base URL must use HTTPS, except for loopback HTTP endpoints"
        )

    netloc = parsed.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    normalized = SplitResult(parsed.scheme, netloc, parsed.path.rstrip("/"), "", "")
    return urlunsplit(normalized)
