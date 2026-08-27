from __future__ import annotations

import os
from pathlib import Path

import pytest

from leanharness.models import ToolCall
from leanharness.tools import ToolRegistry


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=f"call-{name}", name=name, arguments=arguments)


def test_list_is_bounded_recursive_and_skips_heavy_directories(tmp_path: Path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "app.py").write_text("print('ready')", encoding="utf-8")
    (tmp_path / "src" / "nested" / "deep.py").write_text("pass", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.execute(call("workspace_list", path=".", max_depth=3))

    assert result.ok is True
    assert result.data is not None
    paths = [entry["path"] for entry in result.data["entries"]]
    assert paths == ["src", "src/app.py", "src/nested", "src/nested/deep.py"]
    assert all("node_modules" not in path for path in paths)
    assert result.public_metadata == {"path": ".", "entries": 4, "truncated": False}


def test_read_returns_numbered_unicode_range_without_modifying_file(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    original = "first\n世界\nthird\nfourth\n"
    source.write_text(original, encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.execute(
        call("workspace_read", path="source.py", start_line=2, line_count=2)
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["content"] == "2: 世界\n3: third"
    assert result.data["line_count"] == 2
    assert result.data["truncated"] is True
    assert len(result.data["sha256"]) == 64
    assert source.read_text(encoding="utf-8") == original


def test_read_applies_line_and_byte_bounds(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text(("x" * 400 + "\n") * 500, encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.execute(call("workspace_read", path="large.txt", line_count=400))

    assert result.ok is True
    assert result.data is not None
    assert result.data["line_count"] < 400
    assert result.data["truncated"] is True
    assert len(result.data["content"].encode("utf-8")) <= 64 * 1024 + 2_000


def test_search_is_literal_case_insensitive_and_skips_dependencies(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def Target():\n    return '[literal]'\n", encoding="utf-8"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("TARGET", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    insensitive = registry.execute(call("workspace_search", query="target", path="."))
    literal = registry.execute(call("workspace_search", query="[literal]", path="src"))

    assert insensitive.ok is True and insensitive.data is not None
    assert insensitive.data["matches"] == [
        {"path": "src/app.py", "line": 1, "preview": "def Target():"}
    ]
    assert literal.ok is True and literal.data is not None
    assert literal.data["matches"][0]["line"] == 2
    assert all("node_modules" not in item["path"] for item in insensitive.data["matches"])


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("workspace_read", {"path": "missing.py"}, "PATH_NOT_FOUND"),
        ("workspace_read", {"path": "../outside.py"}, "PATH_OUTSIDE_WORKSPACE"),
        ("workspace_read", {"path": "ok.py", "unknown": True}, "TOOL_INVALID_ARGUMENTS"),
        ("workspace_search", {"query": ""}, "TOOL_INVALID_ARGUMENTS"),
        ("unknown_tool", {}, "TOOL_NOT_FOUND"),
    ],
)
def test_tool_errors_are_structured_and_recoverable(
    tmp_path: Path, name: str, arguments: dict[str, object], code: str
) -> None:
    (tmp_path / "ok.py").write_text("pass", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.execute(ToolCall(id="call-1", name=name, arguments=arguments))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == code
    assert result.error.recoverable is True
    assert result.to_model_dict()["error"]["code"] == code


@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("binary.dat", b"head\x00tail", "BINARY_FILE"),
        ("legacy.txt", b"\xff\xfe", "NON_UTF8_FILE"),
    ],
)
def test_read_rejects_binary_and_non_utf8_files(
    tmp_path: Path, filename: str, content: bytes, code: str
) -> None:
    (tmp_path / filename).write_bytes(content)

    result = ToolRegistry(tmp_path).execute(call("workspace_read", path=filename))

    assert result.ok is False
    assert result.error is not None and result.error.code == code


def test_absolute_path_is_rejected_even_when_it_points_inside_workspace(tmp_path: Path) -> None:
    source = tmp_path / "inside.py"
    source.write_text("pass", encoding="utf-8")

    result = ToolRegistry(tmp_path).execute(call("workspace_read", path=str(source)))

    assert result.ok is False
    assert result.error is not None and result.error.code == "PATH_OUTSIDE_WORKSPACE"


def test_symlink_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = tmp_path / "outside-link.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("Creating symlinks is not available on this Windows account")

    result = ToolRegistry(tmp_path).execute(call("workspace_read", path=link.name))

    assert result.ok is False
    assert result.error is not None and result.error.code == "PATH_OUTSIDE_WORKSPACE"


def test_tool_definitions_expose_only_read_capabilities(tmp_path: Path) -> None:
    definitions = ToolRegistry(tmp_path).definitions

    assert [definition.name for definition in definitions] == [
        "workspace_list",
        "workspace_read",
        "workspace_search",
    ]
