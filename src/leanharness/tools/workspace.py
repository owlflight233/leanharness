"""Workspace-confined listing, reading, and literal-search tools."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanharness.models import ToolDefinition
from leanharness.tools.contracts import ToolExecutionError, ToolResult

MAX_READ_BYTES = 64 * 1024
MAX_READ_LINES = 400
MAX_LIST_DEPTH = 4
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_FILES = 10_000
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 256
SKIPPED_DIRECTORIES = frozenset(
    {".git", ".venv", "node_modules", "dist", "build", "coverage", "__pycache__", ".cache"}
)


@dataclass(frozen=True, slots=True)
class WorkspaceBoundary:
    root: Path

    @classmethod
    def create(cls, root: Path) -> WorkspaceBoundary:
        return cls(root=root.resolve(strict=True))

    def resolve(self, value: object, *, expected: str) -> tuple[Path, str]:
        path_text = _require_string(value, "path")
        path = Path(path_text)
        if path.is_absolute() or path.drive or ".." in path.parts:
            raise ToolExecutionError(
                "PATH_OUTSIDE_WORKSPACE", "Path must stay inside the workspace"
            )
        try:
            resolved = (self.root / path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolExecutionError("PATH_NOT_FOUND", f"Path does not exist: {path_text}") from exc
        if not resolved.is_relative_to(self.root):
            raise ToolExecutionError(
                "PATH_OUTSIDE_WORKSPACE", "Path must stay inside the workspace"
            )
        if expected == "file" and not resolved.is_file():
            raise ToolExecutionError("PATH_NOT_FILE", f"Path is not a file: {path_text}")
        if expected == "directory" and not resolved.is_dir():
            raise ToolExecutionError("PATH_NOT_DIRECTORY", f"Path is not a directory: {path_text}")
        relative = resolved.relative_to(self.root).as_posix()
        return resolved, relative or "."


class WorkspaceListTool:
    definition = ToolDefinition(
        name="workspace_list",
        description=(
            "List files and directories inside the workspace without reading file contents."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory path."},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_DEPTH},
            },
            "additionalProperties": False,
        },
    )

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def execute(self, tool_call_id: str, arguments: dict[str, Any]) -> ToolResult:
        _reject_unknown(arguments, {"path", "max_depth"})
        root, relative_root = self._boundary.resolve(
            arguments.get("path", "."), expected="directory"
        )
        max_depth = _bounded_int(
            arguments.get("max_depth", 1), "max_depth", 1, MAX_LIST_DEPTH
        )
        entries: list[dict[str, object]] = []
        truncated = _collect_entries(self._boundary, root, max_depth, entries)
        return ToolResult(
            tool_call_id=tool_call_id,
            tool=self.definition.name,
            ok=True,
            data={"path": relative_root, "entries": entries, "truncated": truncated},
            public_metadata={
                "path": relative_root,
                "entries": len(entries),
                "truncated": truncated,
            },
        )


class WorkspaceReadTool:
    definition = ToolDefinition(
        name="workspace_read",
        description="Read a bounded range of lines from one UTF-8 text file in the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path."},
                "start_line": {"type": "integer", "minimum": 1},
                "line_count": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINES},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def execute(self, tool_call_id: str, arguments: dict[str, Any]) -> ToolResult:
        _reject_unknown(arguments, {"path", "start_line", "line_count"})
        path, relative = self._boundary.resolve(arguments.get("path"), expected="file")
        start = _bounded_int(arguments.get("start_line", 1), "start_line", 1, 10_000_000)
        count = _bounded_int(
            arguments.get("line_count", 200), "line_count", 1, MAX_READ_LINES
        )
        selected, truncated = _read_lines(path, start, count)
        content = "\n".join(f"{number}: {line}" for number, line in selected)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return ToolResult(
            tool_call_id=tool_call_id,
            tool=self.definition.name,
            ok=True,
            data={
                "path": relative,
                "start_line": start,
                "line_count": len(selected),
                "content": content,
                "truncated": truncated,
                "sha256": digest,
            },
            public_metadata={
                "path": relative,
                "start_line": start,
                "line_count": len(selected),
                "truncated": truncated,
            },
        )


class WorkspaceSearchTool:
    definition = ToolDefinition(
        name="workspace_search",
        description="Search UTF-8 workspace files for a bounded literal string.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "path": {"type": "string", "description": "Relative directory or file path."},
                "case_sensitive": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def execute(self, tool_call_id: str, arguments: dict[str, Any]) -> ToolResult:
        _reject_unknown(arguments, {"query", "path", "case_sensitive", "max_results"})
        query = _require_string(arguments.get("query"), "query")
        if len(query) > MAX_QUERY_CHARS:
            raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", "query exceeds 256 characters")
        case_sensitive = arguments.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", "case_sensitive must be boolean")
        limit = _bounded_int(
            arguments.get("max_results", MAX_SEARCH_RESULTS),
            "max_results",
            1,
            MAX_SEARCH_RESULTS,
        )
        target, relative = self._boundary.resolve(arguments.get("path", "."), expected="any")
        matches: list[dict[str, object]] = []
        scanned = 0
        skipped = 0
        stopped_for_file_limit = False
        for file_path in _iter_search_files(self._boundary, target):
            if scanned >= MAX_SEARCH_FILES:
                stopped_for_file_limit = True
                break
            if len(matches) >= limit:
                break
            scanned += 1
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    skipped += 1
                    continue
                raw = file_path.read_bytes()
                text = _decode_text(raw, file_path.name)
            except (OSError, ToolExecutionError):
                skipped += 1
                continue
            needle = query if case_sensitive else query.casefold()
            for line_number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(
                        {
                            "path": file_path.relative_to(self._boundary.root).as_posix(),
                            "line": line_number,
                            "preview": line[:240],
                        }
                    )
                    if len(matches) >= limit:
                        break
        truncated = len(matches) >= limit or stopped_for_file_limit
        return ToolResult(
            tool_call_id=tool_call_id,
            tool=self.definition.name,
            ok=True,
            data={
                "query": query,
                "path": relative,
                "matches": matches,
                "files_scanned": scanned,
                "files_skipped": skipped,
                "truncated": truncated,
            },
            public_metadata={
                "path": relative,
                "matches": len(matches),
                "files_scanned": scanned,
                "truncated": truncated,
            },
        )


def _collect_entries(
    boundary: WorkspaceBoundary,
    directory: Path,
    max_depth: int,
    output: list[dict[str, object]],
    depth: int = 1,
) -> bool:
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ToolExecutionError("TOOL_EXECUTION_FAILED", "Directory could not be read") from exc
    for child in children:
        if len(output) >= MAX_LIST_ENTRIES:
            return True
        if child.name in SKIPPED_DIRECTORIES:
            continue
        try:
            resolved = child.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(boundary.root):
            continue
        relative = resolved.relative_to(boundary.root).as_posix()
        kind = "directory" if resolved.is_dir() else "file" if resolved.is_file() else "other"
        output.append({"path": relative, "type": kind})
        if (
            kind == "directory"
            and depth < max_depth
            and _collect_entries(boundary, resolved, max_depth, output, depth + 1)
        ):
            return True
    return False


def _iter_search_files(boundary: WorkspaceBoundary, target: Path):
    if target.is_file():
        yield target
        return
    stack = [target]
    visited: set[Path] = set()
    while stack:
        directory = stack.pop()
        if directory in visited:
            continue
        visited.add(directory)
        try:
            entries = sorted(
                os.scandir(directory), key=lambda entry: entry.name.casefold(), reverse=True
            )
        except OSError:
            continue
        for entry in entries:
            if entry.name in SKIPPED_DIRECTORIES:
                continue
            try:
                resolved = Path(entry.path).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_relative_to(boundary.root):
                continue
            if resolved.is_dir():
                stack.append(resolved)
            elif resolved.is_file():
                yield resolved


def _read_lines(path: Path, start: int, count: int) -> tuple[list[tuple[int, str]], bool]:
    output: list[tuple[int, str]] = []
    output_bytes = 0
    truncated = False
    try:
        with path.open("r", encoding="utf-8", errors="strict", newline=None) as handle:
            for line_number, line in enumerate(handle, 1):
                if "\x00" in line:
                    raise ToolExecutionError("BINARY_FILE", f"File is not UTF-8 text: {path.name}")
                if line_number < start:
                    continue
                clean_line = line.rstrip("\r\n")
                encoded_size = len(clean_line.encode("utf-8")) + 1
                if len(output) >= count or output_bytes + encoded_size > MAX_READ_BYTES:
                    truncated = True
                    break
                output.append((line_number, clean_line))
                output_bytes += encoded_size
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("NON_UTF8_FILE", f"File is not UTF-8 text: {path.name}") from exc
    except OSError as exc:
        raise ToolExecutionError("TOOL_EXECUTION_FAILED", "File could not be read") from exc
    return output, truncated


def _decode_text(raw: bytes, name: str) -> str:
    if b"\x00" in raw:
        raise ToolExecutionError("BINARY_FILE", f"File is not UTF-8 text: {name}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("NON_UTF8_FILE", f"File is not UTF-8 text: {name}") from exc


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", f"{name} must be a non-empty string")
    return value


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolExecutionError(
            "TOOL_INVALID_ARGUMENTS", f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    if unknown := sorted(set(arguments) - allowed):
        raise ToolExecutionError(
            "TOOL_INVALID_ARGUMENTS", f"Unknown arguments: {', '.join(unknown)}"
        )
