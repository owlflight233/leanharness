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
