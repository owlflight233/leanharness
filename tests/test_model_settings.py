from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanharness.application.model_settings import (
    LocalModelSettings,
    LocalModelSettingsStore,
    get_effective_model_status,
    load_effective_model_config,
)
from leanharness.errors import ModelNotConfiguredError


def test_local_settings_survive_restart_without_persisting_credentials(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    path = LocalModelSettingsStore(data_dir).save(
        LocalModelSettings(
            base_url="https://api.deepseek.com/",
            model="deepseek-v4-flash-vision-exp",
            thinking=True,
            reasoning_effort="high",
        )
    )

    config = load_effective_model_config(
        data_dir,
        environ={"DEEPSEEK_API_KEY": "secret-from-process"},
    )
    serialized = path.read_text(encoding="utf-8")

    assert config.base_url == "https://api.deepseek.com"
    assert config.model == "deepseek-v4-flash-vision-exp"
    assert config.api_key == "secret-from-process"
    assert config.thinking is True
    assert "secret-from-process" not in serialized
    assert "api_key" not in serialized


def test_environment_overrides_non_secret_local_settings(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    LocalModelSettingsStore(data_dir).save(
        LocalModelSettings("https://local.example.test/v1", "local-model")
    )

    config = load_effective_model_config(
        data_dir,
        environ={
            "LEANHARNESS_MODEL_BASE_URL": "https://override.example.test/v1",
            "LEANHARNESS_MODEL_NAME": "override-model",
            "LEANHARNESS_MODEL_THINKING": "disabled",
        },
    )

    assert config.base_url == "https://override.example.test/v1"
    assert config.model == "override-model"
    assert config.thinking is False


def test_invalid_local_settings_fail_without_echoing_file_content(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    secret = "must-not-be-echoed"
    (data_dir / "model.json").write_text(
        json.dumps({"base_url": secret, "unexpected": True}),
        encoding="utf-8",
    )

    with pytest.raises(ModelNotConfiguredError) as raised:
        load_effective_model_config(data_dir, environ={})

    assert secret not in str(raised.value)
    assert get_effective_model_status(data_dir, environ={}).configured is False
