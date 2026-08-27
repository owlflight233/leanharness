from pathlib import Path

import pytest

from leanharness.config import DEFAULT_HOST, DEFAULT_PORT, build_config, resolve_workspace
from leanharness.errors import ConfigurationError, WorkspaceError


def test_resolve_workspace_defaults_to_supplied_cwd(tmp_path: Path) -> None:
    assert resolve_workspace(cwd=tmp_path) == tmp_path.resolve()


def test_resolve_workspace_accepts_an_explicit_directory(tmp_path: Path) -> None:
    nested = tmp_path / "project"
    nested.mkdir()

    assert resolve_workspace(str(nested)) == nested.resolve()


def test_resolve_workspace_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="does not exist"):
        resolve_workspace(tmp_path / "missing")


def test_resolve_workspace_rejects_file(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a workspace", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="not a directory"):
        resolve_workspace(file_path)


def test_build_config_applies_defaults(tmp_path: Path) -> None:
    config = build_config(workspace=tmp_path)

    assert config.workspace == tmp_path.resolve()
    assert config.host == DEFAULT_HOST
    assert config.port == DEFAULT_PORT
    assert config.log_level == "INFO"


@pytest.mark.parametrize("port", [0, 65536])
def test_build_config_rejects_invalid_port(tmp_path: Path, port: int) -> None:
    with pytest.raises(ConfigurationError, match="Port"):
        build_config(workspace=tmp_path, port=port)


def test_build_config_normalizes_log_level(tmp_path: Path) -> None:
    config = build_config(workspace=tmp_path, log_level=" warning ")

    assert config.log_level == "WARNING"
