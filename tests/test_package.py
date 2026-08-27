import argparse
from pathlib import Path

import pytest

from leanharness import __version__
from leanharness.cli.doctor import DiagnosticCheck
from leanharness.cli.main import build_parser, main


def test_development_version_is_exposed() -> None:
    assert __version__ == "0.1.0.dev0"


def test_empty_cli_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "foundation milestone" in captured.out


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
