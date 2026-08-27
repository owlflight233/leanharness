import argparse
from pathlib import Path

import pytest

from leanharness import __version__
from leanharness.application.model_gateway import ModelCheckResult
from leanharness.cli.doctor import DiagnosticCheck
from leanharness.cli.main import build_parser, main
from leanharness.errors import ModelAuthError
from leanharness.models import ModelConfig, ModelEvent, ModelUsage


def test_development_version_is_exposed() -> None:
    assert __version__ == "0.1.0.dev0"


def test_empty_cli_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "Local-first coding agent runtime" in captured.out


def test_serve_parser_applies_local_defaults() -> None:
    args = build_parser().parse_args(["serve"])

    assert isinstance(args, argparse.Namespace)
    assert args.host == "127.0.0.1"
    assert args.port == 4318
    assert args.workspace is None


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == "LeanHarness 0.1.0.dev0"


def test_doctor_returns_success_for_passing_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checks = (DiagnosticCheck(name="python", ok=True, detail="Python 3.12"),)
    monkeypatch.setattr("leanharness.cli.main.collect_diagnostics", lambda _workspace: checks)

    assert main(["doctor", "--workspace", str(tmp_path)]) == 0
    assert "[PASS] python: Python 3.12" in capsys.readouterr().out


def test_invalid_workspace_is_a_safe_cli_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing"

    assert main(["doctor", "--workspace", str(missing)]) == 2
    error = capsys.readouterr().err
    assert "INVALID_WORKSPACE" in error
    assert str(missing) in error


def test_model_check_cli_reports_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def successful_check() -> ModelCheckResult:
        return ModelCheckResult(status="ok", model="example-model", latency_ms=12)

    monkeypatch.setattr("leanharness.cli.main.check_model", successful_check)

    assert main(["model", "check"]) == 0
    assert "example-model (12 ms)" in capsys.readouterr().out


def test_model_check_cli_maps_remote_failure_to_exit_three(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failed_check() -> ModelCheckResult:
        raise ModelAuthError("Model authentication failed")

    monkeypatch.setattr("leanharness.cli.main.check_model", failed_check)

    assert main(["model", "check"]) == 3
    assert "MODEL_AUTH_FAILED" in capsys.readouterr().err


def test_chat_cli_prints_content_and_usage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_stream(message: str, *, config: ModelConfig):
        assert message == "hello"
        assert config.model == "example"
        yield ModelEvent(type="turn.started", sequence=0)
        yield ModelEvent(type="content.delta", sequence=1, content="world")
        yield ModelEvent(
            type="usage.reported",
            sequence=2,
            usage=ModelUsage(total_tokens=4),
        )
        yield ModelEvent(type="turn.completed", sequence=3, finish_reason="stop")

    monkeypatch.setenv("LEANHARNESS_MODEL_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LEANHARNESS_MODEL_NAME", "example")
    monkeypatch.setattr("leanharness.cli.main.stream_chat", fake_stream)

    assert main(["chat", "hello"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "world\n"
    assert "usage: 4 tokens" in captured.err
