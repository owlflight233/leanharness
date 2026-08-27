from leanharness import __version__
from leanharness.cli.main import main


def test_development_version_is_exposed() -> None:
    assert __version__ == "0.1.0.dev0"


def test_empty_cli_prints_help(capsys: object) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "foundation milestone" in captured.out
