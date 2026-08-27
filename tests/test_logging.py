import json
import logging

from leanharness.logging import JsonFormatter, redact_text


def test_redact_text_removes_common_secret_shapes() -> None:
    source = "Authorization: Bearer abc123 api_key=secret gho_abcdefghijklmnop"

    result = redact_text(source)

    assert "abc123" not in result
    assert "secret" not in result
    assert "gho_abcdefghijklmnop" not in result
    assert result.count("[REDACTED]") == 3


def test_json_formatter_emits_structured_record() -> None:
    record = logging.LogRecord(
        name="leanharness.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ready",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "leanharness.test"
    assert payload["message"] == "ready"
    assert "timestamp" in payload
