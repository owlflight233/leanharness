from dataclasses import FrozenInstanceError

import pytest

from leanharness.errors import ModelNotConfiguredError
from leanharness.models import (
    ModelConfig,
    ModelEvent,
    ModelUsage,
    get_model_config_status,
    load_model_config,
)


def test_model_config_loads_complete_environment_without_exposing_key() -> None:
    secret = "test-secret-key-value"
    config = load_model_config(
        {
            "LEANHARNESS_MODEL_BASE_URL": "https://api.deepseek.com/",
            "LEANHARNESS_MODEL_NAME": "deepseek-chat",
            "LEANHARNESS_MODEL_API_KEY": secret,
        }
    )

    assert config.base_url == "https://api.deepseek.com"
    assert config.chat_completions_url == "https://api.deepseek.com/chat/completions"
    assert config.api_key == secret
    assert secret not in repr(config)


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.com/v1",
        "https://user:password@models.example.com/v1",
        "https://models.example.com/v1?key=value",
        "https://models.example.com/v1#fragment",
        "not-a-url",
    ],
)
def test_model_config_rejects_unsafe_base_urls(url: str) -> None:
    with pytest.raises(ModelNotConfiguredError):
        load_model_config(
            {
                "LEANHARNESS_MODEL_BASE_URL": url,
                "LEANHARNESS_MODEL_NAME": "example-model",
            }
        )


@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "http://127.0.0.1:8000"])
def test_model_config_allows_loopback_http(url: str) -> None:
    assert (
        load_model_config(
            {
                "LEANHARNESS_MODEL_BASE_URL": url,
                "LEANHARNESS_MODEL_NAME": "local-model",
            }
        ).base_url
        == url
    )


def test_incomplete_config_has_safe_status_and_stable_error() -> None:
    values = {
        "LEANHARNESS_MODEL_NAME": "deepseek-chat",
        "LEANHARNESS_MODEL_API_KEY": "must-not-appear",
    }

    status = get_model_config_status(values)

    assert status.configured is False
    assert status.model == "deepseek-chat"
    with pytest.raises(ModelNotConfiguredError) as caught:
        load_model_config(values)
    assert caught.value.code == "MODEL_NOT_CONFIGURED"
    assert "must-not-appear" not in str(caught.value)


def test_model_config_is_immutable() -> None:
    config = ModelConfig(base_url="https://example.com/v1", model="example")
    with pytest.raises(FrozenInstanceError):
        config.model = "changed"  # type: ignore[misc]


def test_model_event_serializes_only_relevant_fields() -> None:
    usage = ModelUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5)

    assert ModelEvent(type="content.delta", sequence=1, content="hello").to_dict() == {
        "type": "content.delta",
        "sequence": 1,
        "content": "hello",
    }
    assert ModelEvent(type="usage.reported", sequence=2, usage=usage).to_dict() == {
        "type": "usage.reported",
        "sequence": 2,
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
