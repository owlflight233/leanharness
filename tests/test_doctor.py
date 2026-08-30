from pathlib import Path

from leanharness.cli.doctor import collect_diagnostics


def test_collect_diagnostics_reports_known_dependencies(tmp_path: Path) -> None:
    versions = {
        "git": "git version 2.0",
        "node": "v22.0.0",
        "pnpm": "11.0.0",
    }

    checks = collect_diagnostics(tmp_path, command_probe=versions.get)
    by_name = {check.name: check for check in checks}

    assert by_name["python"].ok
    assert by_name["git"].detail == "git version 2.0"
    assert by_name["node"].ok
    assert by_name["pnpm"].ok
    assert by_name["workspace-readable"].ok
    assert by_name["workspace-writable"].ok


def test_collect_diagnostics_fails_a_missing_dependency(tmp_path: Path) -> None:
    checks = collect_diagnostics(tmp_path, command_probe=lambda _name: None)

    assert not next(check for check in checks if check.name == "git").ok


def test_collect_diagnostics_does_not_block_before_model_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LEANHARNESS_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("LEANHARNESS_MODEL_NAME", raising=False)

    checks = collect_diagnostics(
        tmp_path,
        command_probe=lambda name: f"{name} version",
        data_dir=tmp_path / "data",
    )

    model = next(check for check in checks if check.name == "model-config")
    assert model.ok is True
    assert "optional" in model.detail


def test_collect_diagnostics_reports_persistent_model_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from leanharness.application.model_settings import (
        LocalModelSettings,
        LocalModelSettingsStore,
    )

    data_dir = tmp_path / "data"
    monkeypatch.delenv("LEANHARNESS_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("LEANHARNESS_MODEL_NAME", raising=False)
    LocalModelSettingsStore(data_dir).save(
        LocalModelSettings("https://api.deepseek.com", "deepseek-test")
    )

    checks = collect_diagnostics(
        tmp_path,
        command_probe=lambda name: f"{name} version",
        data_dir=data_dir,
    )

    model = next(check for check in checks if check.name == "model-config")
    assert model.ok is True
    assert model.detail == "deepseek-test"
