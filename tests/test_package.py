import argparse
from pathlib import Path

import pytest

from leanharness import __version__
from leanharness.application.model_gateway import ModelCheckResult
from leanharness.cli.doctor import DiagnosticCheck
from leanharness.cli.main import build_parser, main
from leanharness.errors import ModelAuthError
from leanharness.runtime import RuntimeEvent


def test_development_version_is_exposed() -> None:
    assert __version__ == "0.1.0.dev0"


def test_empty_cli_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "Local-first coding agent runtime" in captured.out


def test_cli_configures_utf8_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    class Stream:
        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    monkeypatch.setattr("leanharness.cli.main.sys.stdout", Stream())
    monkeypatch.setattr("leanharness.cli.main.sys.stderr", Stream())

    from leanharness.cli.main import _configure_stdio

    _configure_stdio()
    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]


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


def test_run_parser_applies_read_only_defaults() -> None:
    args = build_parser().parse_args(["run", "inspect"])

    assert args.workspace is None
    assert args.max_steps == 24


def test_session_parser_supports_lifecycle_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["session", "new", "--permission", "approve"]).permission == "approve"
    renamed = parser.parse_args(["session", "rename", "session-id", "New title"])
    assert renamed.session_id == "session-id"
    assert renamed.title == "New title"


def test_run_cli_separates_trace_and_final_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRuntime:
        async def run(self, task: str):
            assert task == "inspect"
            yield RuntimeEvent(
                type="assistant.progress",
                sequence=0,
                run_id="r1",
                step=1,
                summary="Inspecting files",
            )
            yield RuntimeEvent(
                type="tool.completed",
                sequence=1,
                run_id="r1",
                step=1,
                tool="workspace_list",
                metadata={"ok": True},
            )
            yield RuntimeEvent(
                type="run.completed",
                sequence=2,
                run_id="r1",
                answer="Final answer",
            )

    monkeypatch.setattr(
        "leanharness.cli.main.create_coding_run",
        lambda *_args, **_kwargs: FakeRuntime(),
    )

    assert (
        main(
            [
                "run",
                "inspect",
                "--workspace",
                str(tmp_path),
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == "Final answer\n"
    assert "Inspecting files" in captured.err
    assert "workspace_list" in captured.err


def test_run_cli_answers_model_requested_question(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRuntime:
        def __init__(self, coordinator, run_id: str, session_id: str) -> None:
            self.coordinator = coordinator
            self.run_id = run_id
            self.session_id = session_id

        async def run(self, _task: str):
            request = self.coordinator.request(
                run_id=self.run_id,
                session_id=self.session_id,
                tool_call_id="call-1",
                question="选择目标",
                options=(),
            )
            yield RuntimeEvent(
                type="input.required",
                sequence=0,
                run_id=self.run_id,
                tool="request_user_input",
                metadata={
                    "input_id": request.id,
                    "question": "选择目标",
                    "options": [
                        {"label": "API", "description": "修改后端"},
                        {"label": "Web", "description": "修改前端"},
                    ],
                },
            )
            answer = await self.coordinator.wait(request)
            yield RuntimeEvent(
                type="input.resolved",
                sequence=1,
                run_id=self.run_id,
                tool="request_user_input",
            )
            yield RuntimeEvent(
                type="run.completed",
                sequence=2,
                run_id=self.run_id,
                answer=f"选择了 {answer}。",
            )

    def create_runtime(*_args, **kwargs):
        return FakeRuntime(
            kwargs["user_inputs"],
            kwargs["run_id"],
            kwargs["session_id"],
        )

    monkeypatch.setattr("leanharness.cli.main.create_coding_run", create_runtime)
    monkeypatch.setattr("builtins.input", lambda _prompt: "API")

    args = [
        "run",
        "choose",
        "--workspace",
        str(tmp_path),
        "--data-dir",
        str(tmp_path / "data"),
    ]
    assert main(args) == 0
    output = capsys.readouterr()
    assert "选择了 API。" in output.out
    assert "选择目标" in output.err
    assert "API: 修改后端" in output.err


@pytest.mark.parametrize(
    ("terminal_type", "error_code", "expected"),
    [
        ("run.incomplete", None, 4),
        ("run.failed", "RUN_STALLED", 4),
        ("run.failed", "MODEL_UNAVAILABLE", 3),
        ("run.cancelled", None, 130),
    ],
)
def test_run_cli_maps_terminal_status_to_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_type: str,
    error_code: str | None,
    expected: int,
) -> None:
    class FakeRuntime:
        async def run(self, task: str):
            yield RuntimeEvent(
                type=terminal_type,
                sequence=0,
                run_id="r1",
                summary="not complete",
                error_code=error_code,
                error_message="failure" if error_code else None,
            )

    monkeypatch.setattr(
        "leanharness.cli.main.create_coding_run",
        lambda *_args, **_kwargs: FakeRuntime(),
    )

    assert (
        main(
            [
                "run",
                "inspect",
                "--workspace",
                str(tmp_path),
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )
        == expected
    )
