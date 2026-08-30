"""Non-secret local model settings shared by CLI and Web entry points."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from leanharness.errors import ConfigurationError, ModelNotConfiguredError
from leanharness.models import ModelConfig, ModelConfigStatus
from leanharness.models.config import (
    MODEL_BASE_URL_ENV,
    MODEL_NAME_ENV,
    MODEL_REASONING_EFFORT_ENV,
    MODEL_THINKING_ENV,
    get_model_config_status,
    load_model_config,
)
from leanharness.storage import default_data_dir

MODEL_SETTINGS_FILE = "model.json"
_SETTINGS_KEYS = frozenset({"base_url", "model", "thinking", "reasoning_effort"})


@dataclass(frozen=True, slots=True)
class LocalModelSettings:
    """Persistable model choices; credentials are deliberately absent."""

    base_url: str
    model: str
    thinking: bool = True
    reasoning_effort: str | None = "high"

    def as_environment_defaults(self) -> dict[str, str]:
        return {
            MODEL_BASE_URL_ENV: self.base_url,
            MODEL_NAME_ENV: self.model,
            MODEL_THINKING_ENV: "enabled" if self.thinking else "disabled",
            MODEL_REASONING_EFFORT_ENV: self.reasoning_effort or "",
        }


class LocalModelSettingsStore:
    """Read and atomically write one credential-free JSON settings file."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = Path(data_dir).expanduser() if data_dir is not None else default_data_dir()
        self.path = root.resolve() / MODEL_SETTINGS_FILE

    def load(self) -> LocalModelSettings | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ModelNotConfiguredError("Local model settings could not be read") from exc
        if not isinstance(value, dict) or set(value) - _SETTINGS_KEYS:
            raise ModelNotConfiguredError("Local model settings have an invalid format")
        base_url = value.get("base_url")
        model = value.get("model")
        thinking = value.get("thinking", True)
        reasoning_effort = value.get("reasoning_effort", "high")
        if (
            not isinstance(base_url, str)
            or not isinstance(model, str)
            or not isinstance(thinking, bool)
            or (reasoning_effort is not None and not isinstance(reasoning_effort, str))
        ):
            raise ModelNotConfiguredError("Local model settings have an invalid format")
        settings = LocalModelSettings(
            base_url=base_url.strip(),
            model=model.strip(),
            thinking=thinking,
            reasoning_effort=reasoning_effort.strip() if reasoning_effort else None,
        )
        load_model_config({}, defaults=settings.as_environment_defaults())
        return settings

    def save(self, settings: LocalModelSettings) -> Path:
        validated = load_model_config({}, defaults=settings.as_environment_defaults())
        payload = {
            "base_url": validated.base_url,
            "model": validated.model,
            "thinking": validated.thinking,
            "reasoning_effort": validated.reasoning_effort,
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise ConfigurationError("Local model settings could not be saved") from exc
        return self.path


def load_effective_model_config(
    data_dir: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ModelConfig:
    settings = LocalModelSettingsStore(data_dir).load()
    defaults = settings.as_environment_defaults() if settings is not None else None
    return load_model_config(environ, defaults=defaults)


def get_effective_model_status(
    data_dir: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ModelConfigStatus:
    try:
        settings = LocalModelSettingsStore(data_dir).load()
    except ModelNotConfiguredError:
        return ModelConfigStatus(configured=False)
    defaults = settings.as_environment_defaults() if settings is not None else None
    return get_model_config_status(environ, defaults=defaults)
