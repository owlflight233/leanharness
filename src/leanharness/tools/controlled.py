"""Guarded patch, command, and Git inspection tools."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from leanharness.models import ToolDefinition
from leanharness.tools.contracts import ToolExecutionError, ToolResult
from leanharness.tools.workspace import WorkspaceBoundary, _bounded_int, _reject_unknown

MAX_PATCH_BYTES = 256 * 1024
MAX_PATCH_FILES = 50
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PATCH_RESULT_BYTES = 8 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
DEFAULT_COMMAND_TIMEOUT = 120
MAX_COMMAND_TIMEOUT = 600
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: tuple[tuple[int, int, int, int, tuple[str, ...]], ...]

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


class WorkspaceMkdirTool:
    definition = ToolDefinition(
        name="workspace_mkdir",
        description=(
            "Create a new directory inside the workspace. Use a relative path only. "
            "Set parents=true to create missing parent directories. Existing paths, "
            "symbolic links, and paths outside the workspace are rejected."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory path."},
                "parents": {"type": "boolean", "default": True},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def preview(self, arguments: dict[str, Any]) -> dict[str, object]:
        path, relative, parents = self._validate(arguments)
        if path.exists():
            raise ToolExecutionError("PATH_ALREADY_EXISTS", f"Path already exists: {relative}")
        return {"path": relative, "parents": parents}

    def execute(self, tool_call_id: str, arguments: dict[str, Any]) -> ToolResult:
        target, relative, parents = self._validate(arguments)
        if target.exists():
            raise ToolExecutionError("PATH_ALREADY_EXISTS", f"Path already exists: {relative}")

        created: list[Path] = []
        current = self._boundary.root
        try:
            for index, part in enumerate(Path(relative).parts):
                current = current / part
                is_target = index == len(Path(relative).parts) - 1
                if current.is_symlink():
                    raise ToolExecutionError(
                        "PATH_SYMLINK", "Symbolic links cannot be modified"
                    )
                if current.exists():
                    if not current.is_dir():
                        raise ToolExecutionError(
                            "PATH_NOT_DIRECTORY", f"Path component is not a directory: {part}"
                        )
                    if is_target:
                        raise ToolExecutionError(
                            "PATH_ALREADY_EXISTS", f"Path already exists: {relative}"
                        )
                    continue
                if not parents and not is_target:
                    raise ToolExecutionError(
                        "PATH_NOT_FOUND", "Parent directory does not exist"
                    )
                current.mkdir()
                created.append(current)
        except ToolExecutionError:
            _remove_created_directories(created)
            raise
        except OSError as exc:
            _remove_created_directories(created)
            raise ToolExecutionError(
                "DIRECTORY_CREATE_FAILED",
                "Directory creation failed and was rolled back",
                recoverable=False,
            ) from exc

        created_paths = [path.relative_to(self._boundary.root).as_posix() for path in created]
        metadata = {
            "path": relative,
            "directories_created": len(created_paths),
            "created_paths": created_paths,
        }
        return ToolResult(
            tool_call_id, self.definition.name, True, metadata, public_metadata=metadata
        )

    def _validate(self, arguments: dict[str, Any]) -> tuple[Path, str, bool]:
        _reject_unknown(arguments, {"path", "parents"})
        parents = arguments.get("parents", True)
        if not isinstance(parents, bool):
            raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", "parents must be boolean")
        path, relative = self._boundary.resolve_directory_output(arguments.get("path"))
        return path, relative, parents


class WorkspacePatchTool:
    definition = ToolDefinition(
        name="workspace_patch",
        description=(
            "Apply one bounded unified diff to UTF-8 workspace files. "
            "Renames, binary patches, permission changes, and paths outside "
            "the workspace are rejected."
        ),
        parameters={
            "type": "object",
            "properties": {"patch": {"type": "string", "maxLength": MAX_PATCH_BYTES}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    )

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def preview(self, arguments: dict[str, Any]) -> dict[str, object]:
        patches = self._parse(arguments)
        hashes = self.snapshot_hashes(patches)
        return {
            "files": [item.path for item in patches],
            "file_count": len(patches),
            "target_hashes": hashes,
            "preview": arguments["patch"],
        }

    def snapshot_hashes(self, patches: tuple[FilePatch, ...]) -> dict[str, str | None]:
        hashes: dict[str, str | None] = {}
        for item in patches:
            path, relative = self._boundary.resolve_output(item.path)
            hashes[relative] = _file_hash(path) if path.exists() else None
        return hashes

    def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        expected_hashes: dict[str, str | None] | None = None,
    ) -> ToolResult:
        patches = self._parse(arguments)
        current_hashes = self.snapshot_hashes(patches)
        if expected_hashes is not None and current_hashes != expected_hashes:
            raise ToolExecutionError("PATCH_STALE", "Target files changed after approval")

        prepared: list[tuple[Path, str, bytes | None, bytes | None]] = []
        total = 0
        for item in patches:
            path, relative = self._boundary.resolve_output(item.path)
            old_bytes = path.read_bytes() if path.exists() else None
            if old_bytes is not None:
                _decode_utf8(old_bytes, relative)
            new_bytes = _apply_file_patch(item, old_bytes)
            if new_bytes is not None and len(new_bytes) > MAX_FILE_BYTES:
                raise ToolExecutionError("PATCH_FILE_TOO_LARGE", "Patched file exceeds 2 MiB")
            total += len(new_bytes or b"")
            if total > MAX_PATCH_RESULT_BYTES:
                raise ToolExecutionError("PATCH_RESULT_TOO_LARGE", "Patch result exceeds 8 MiB")
            prepared.append((path, relative, old_bytes, new_bytes))

        created_temps: list[Path] = []
        applied: list[tuple[Path, bytes | None]] = []
        try:
            staged: dict[Path, Path] = {}
            for path, _, _, new_bytes in prepared:
                if new_bytes is None:
                    continue
                descriptor, temp_name = tempfile.mkstemp(prefix=".leanharness-", dir=path.parent)
                temp = Path(temp_name)
                created_temps.append(temp)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(new_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                staged[path] = temp
            for path, _, old_bytes, new_bytes in prepared:
                applied.append((path, old_bytes))
                if new_bytes is None:
                    path.unlink()
                else:
                    os.replace(staged[path], path)
            created_temps.clear()
        except OSError as exc:
            for path, old_bytes in reversed(applied):
                try:
                    if old_bytes is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(old_bytes)
                except OSError:
                    pass
            raise ToolExecutionError(
                "PATCH_WRITE_FAILED", "Patch write failed and was rolled back", recoverable=False
            ) from exc
        finally:
            for temp in created_temps:
                temp.unlink(missing_ok=True)

        created = sum(old is None and new is not None for _, _, old, new in prepared)
        deleted = sum(old is not None and new is None for _, _, old, new in prepared)
        modified = len(prepared) - created - deleted
        metadata = {
            "files": [relative for _, relative, _, _ in prepared],
            "file_count": len(prepared),
            "created": created,
            "modified": modified,
            "deleted": deleted,
        }
        return ToolResult(
            tool_call_id, self.definition.name, True, metadata, public_metadata=metadata
        )

    def _parse(self, arguments: dict[str, Any]) -> tuple[FilePatch, ...]:
        _reject_unknown(arguments, {"patch"})
        patch = arguments.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", "patch must be non-empty text")
        if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise ToolExecutionError("PATCH_TOO_LARGE", "Patch exceeds 256 KiB")
        return _parse_unified_diff(patch)


class WorkspaceCommandTool:
    definition = ToolDefinition(
        name="workspace_command",
        description=(
            "Run a bounded project verification command without a shell. "
            "Allowed profiles are pytest, ruff, python-test, uv-test, pnpm-test, "
            "pnpm-typecheck, pnpm-lint, pnpm-build, and npm equivalents."
        ),
        parameters={
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
            },
            "required": ["profile"],
            "additionalProperties": False,
        },
    )

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def preview(self, arguments: dict[str, Any]) -> dict[str, object]:
        command, timeout = self._validate(arguments)
        return {"profile": arguments["profile"], "command": command, "timeout_seconds": timeout}

    def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        cancel_signal: CancellationSignal | None = None,
    ) -> ToolResult:
        command, timeout = self._validate(arguments)
        environment = _minimal_environment()
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                command,
                cwd=self._boundary.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creation_flags,
            )
            stdout, stderr, stop_reason = _communicate_bounded(
                process,
                timeout=timeout,
                cancel_signal=cancel_signal,
            )
            if stop_reason is not None:
                code, message = (
                    ("COMMAND_CANCELLED", "Command was cancelled")
                    if stop_reason == "cancelled"
                    else ("COMMAND_TIMEOUT", "Command exceeded its timeout")
                )
                return ToolResult(
                    tool_call_id,
                    self.definition.name,
                    False,
                    error=_tool_error(code, message),
                    public_metadata={
                        "profile": arguments["profile"],
                        stop_reason: True,
                    },
                )
        except (OSError, ValueError) as exc:
            raise ToolExecutionError("COMMAND_UNAVAILABLE", "Command could not be started") from exc
        output = {
            "profile": arguments["profile"],
            "exit_code": process.returncode,
            "stdout": _bounded_output(stdout),
            "stderr": _bounded_output(stderr),
            "stdout_truncated": len(stdout) > MAX_COMMAND_OUTPUT_BYTES,
            "stderr_truncated": len(stderr) > MAX_COMMAND_OUTPUT_BYTES,
        }
        metadata = {
            "profile": arguments["profile"],
            "exit_code": process.returncode,
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
        }
        return ToolResult(
            tool_call_id,
            self.definition.name,
            process.returncode == 0,
            data=output,
            error=(
                None
                if process.returncode == 0
                else _tool_error(
                    "COMMAND_FAILED",
                    "Verification command returned a non-zero exit code",
                )
            ),
            public_metadata=metadata,
        )

    def _validate(self, arguments: dict[str, Any]) -> tuple[list[str], int]:
        _reject_unknown(arguments, {"profile", "args", "timeout_seconds"})
        profile = arguments.get("profile")
        if not isinstance(profile, str) or profile not in _COMMAND_PROFILES:
            raise ToolExecutionError("COMMAND_NOT_ALLOWED", "Unknown command profile")
        args = arguments.get("args", [])
        if (
            not isinstance(args, list)
            or len(args) > 32
            or not all(isinstance(v, str) for v in args)
        ):
            raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", "args must be a string array")
        if any(_dangerous_argument(value) for value in args):
            raise ToolExecutionError("COMMAND_ARGUMENT_DENIED", "Command argument is not allowed")
        timeout = _bounded_int(
            arguments.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT),
            "timeout_seconds",
            1,
            MAX_COMMAND_TIMEOUT,
        )
        return [*_COMMAND_PROFILES[profile], *args], timeout


class GitInspectTool:
    definition = ToolDefinition(
        name="git_inspect",
        description=(
            "Inspect local Git status, diff, log, or show output without repository writes. "
            "operation is required. Use path only to scope status, diff, or log to one "
            "workspace-relative file; show uses revision plus an optional path. "
            "Use operation='status' to inspect current changes, operation='diff' for changes, "
            "operation='log' for recent commits, and operation='show' for one revision."
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["status", "diff", "log", "show"]},
                "revision": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    )

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def execute(self, tool_call_id: str, arguments: dict[str, Any]) -> ToolResult:
        _reject_unknown(arguments, {"operation", "revision", "path"})
        operation = arguments.get("operation")
        if operation not in {"status", "diff", "log", "show"}:
            raise ToolExecutionError("GIT_OPERATION_DENIED", "Git operation is not read-only")
        command = ["git"]
        if operation == "status":
            command += ["status", "--short", "--branch"]
        elif operation == "diff":
            command += ["diff", "--no-ext-diff", "--"]
        elif operation == "log":
            command += ["log", "--oneline", "--decorate", "-n", "20"]
        else:
            revision = arguments.get("revision", "HEAD")
            if not isinstance(revision, str) or not revision or revision.startswith("-"):
                raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", "revision is invalid")
            command += ["show", "--no-ext-diff", "--format=medium", revision, "--"]
        relative_path = None
        if "path" in arguments:
            _, relative_path = self._boundary.resolve(arguments["path"], expected="any")
            if operation not in {"status", "diff", "log", "show"}:
                raise ToolExecutionError("TOOL_INVALID_ARGUMENTS", "path is unsupported here")
            if operation in {"status", "log"}:
                command.append("--")
            command.append(relative_path)
        try:
            completed = subprocess.run(
                command,
                cwd=self._boundary.root,
                env=_minimal_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolExecutionError("GIT_UNAVAILABLE", "Git inspection failed") from exc
        stdout = _bounded_output(completed.stdout)
        stderr = _bounded_output(completed.stderr)
        if completed.returncode != 0:
            not_repo = (
                completed.returncode == 128
                and "not a git repository" in completed.stderr.decode(
                    "utf-8", errors="replace"
                ).lower()
            )
            return ToolResult(
                tool_call_id,
                self.definition.name,
                False,
                error=_tool_error(
                    "GIT_NOT_REPOSITORY" if not_repo else "GIT_FAILED",
                    "Workspace is not a Git repository"
                    if not_repo
                    else "Git inspection returned a non-zero exit code",
                ),
                public_metadata={
                    "operation": operation,
                    "exit_code": completed.returncode,
                    **({"repository": False} if not_repo else {}),
                },
            )
        metadata = {
            "operation": operation,
            "path": relative_path,
            "exit_code": completed.returncode,
            "output_bytes": len(completed.stdout),
        }
        return ToolResult(
            tool_call_id,
            self.definition.name,
            True,
            data={
                "output": stdout,
                "stderr": stderr,
                "truncated": len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES,
            },
            public_metadata=metadata,
        )


_COMMAND_PROFILES: dict[str, tuple[str, ...]] = {
    "pytest": ("pytest",),
    "ruff": ("ruff", "check"),
    "python-test": ("python", "-m", "pytest"),
    "uv-test": ("uv", "run", "pytest"),
    "uv-ruff": ("uv", "run", "ruff", "check"),
    "pnpm-test": ("pnpm", "test"),
    "pnpm-typecheck": ("pnpm", "typecheck"),
    "pnpm-lint": ("pnpm", "lint"),
    "pnpm-build": ("pnpm", "build"),
    "npm-test": ("npm", "test", "--"),
    "npm-typecheck": ("npm", "run", "typecheck", "--"),
    "npm-lint": ("npm", "run", "lint", "--"),
    "npm-build": ("npm", "run", "build", "--"),
}


def _remove_created_directories(paths: list[Path]) -> None:
    for path in reversed(paths):
        with suppress(OSError):
            path.rmdir()


def _parse_unified_diff(text: str) -> tuple[FilePatch, ...]:
    lines = text.replace("\r\n", "\n").splitlines()
    output: list[FilePatch] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith(("diff ", "index ")):
            index += 1
            continue
        if not lines[index].startswith("--- "):
            raise ToolExecutionError("PATCH_INVALID", "Expected a unified diff file header")
        old_path = _diff_path(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ToolExecutionError("PATCH_INVALID", "Missing new-file header")
        new_path = _diff_path(lines[index][4:])
        index += 1
        if old_path is not None and new_path is not None and old_path != new_path:
            raise ToolExecutionError("PATCH_RENAME_DENIED", "File renames are not supported")
        hunks = []
        while index < len(lines) and lines[index].startswith("@@ "):
            match = _HUNK.match(lines[index])
            if not match:
                raise ToolExecutionError("PATCH_INVALID", "Malformed hunk header")
            old_start, old_count, new_start, new_count = (
                int(match.group(1)),
                int(match.group(2) or 1),
                int(match.group(3)),
                int(match.group(4) or 1),
            )
            index += 1
            body: list[str] = []
            old_seen = new_seen = 0
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- ", "diff ")):
                line = lines[index]
                if line == "\\ No newline at end of file":
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise ToolExecutionError("PATCH_INVALID", "Malformed hunk line")
                body.append(line)
                old_seen += line[0] in " -"
                new_seen += line[0] in " +"
                index += 1
            if old_seen != old_count or new_seen != new_count:
                raise ToolExecutionError("PATCH_INVALID", "Hunk line counts do not match header")
            hunks.append((old_start, old_count, new_start, new_count, tuple(body)))
        if not hunks:
            raise ToolExecutionError("PATCH_INVALID", "Patch contains no hunks")
        output.append(FilePatch(old_path, new_path, tuple(hunks)))
        if len(output) > MAX_PATCH_FILES:
            raise ToolExecutionError("PATCH_TOO_MANY_FILES", "Patch exceeds 50 files")
    if not output:
        raise ToolExecutionError("PATCH_INVALID", "Patch is empty")
    paths = [item.path.casefold() for item in output]
    if len(paths) != len(set(paths)):
        raise ToolExecutionError("PATCH_INVALID", "Each file may appear only once")
    return tuple(output)


def _diff_path(value: str) -> str | None:
    raw = value.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith("a/") or raw.startswith("b/"):
        raw = raw[2:]
    if not raw or raw.startswith("\""):
        raise ToolExecutionError("PATCH_INVALID", "Quoted or empty paths are unsupported")
    path = Path(raw)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ToolExecutionError("PATH_OUTSIDE_WORKSPACE", "Patch path must stay in workspace")
    return path.as_posix()


def _apply_file_patch(item: FilePatch, old_bytes: bytes | None) -> bytes | None:
    if item.old_path is None and old_bytes is not None:
        raise ToolExecutionError("PATCH_TARGET_EXISTS", "Create target already exists")
    if item.old_path is not None and old_bytes is None:
        raise ToolExecutionError("PATCH_TARGET_MISSING", "Patch target does not exist")
    old_text = _decode_utf8(old_bytes or b"", item.path)
    old_lines = old_text.splitlines()
    result: list[str] = []
    cursor = 0
    for old_start, _, _, _, body in item.hunks:
        target = max(old_start - 1, 0)
        if target < cursor or target > len(old_lines):
            raise ToolExecutionError("PATCH_CONTEXT_MISMATCH", "Patch hunk is out of range")
        result.extend(old_lines[cursor:target])
        cursor = target
        for line in body:
            marker, content = line[0], line[1:]
            if marker in " -":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise ToolExecutionError("PATCH_CONTEXT_MISMATCH", "Patch context is stale")
                if marker == " ":
                    result.append(content)
                cursor += 1
            else:
                result.append(content)
    result.extend(old_lines[cursor:])
    if item.new_path is None:
        return None
    trailing_newline = old_text.endswith(("\n", "\r")) or item.old_path is None
    rendered = "\n".join(result) + ("\n" if trailing_newline and result else "")
    return rendered.encode("utf-8")


def _decode_utf8(value: bytes, name: str) -> str:
    if b"\x00" in value:
        raise ToolExecutionError("BINARY_FILE", f"File is not UTF-8 text: {name}")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("NON_UTF8_FILE", f"File is not UTF-8 text: {name}") from exc


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minimal_environment() -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment["PYTHONUNBUFFERED"] = "1"
    environment["NO_COLOR"] = "1"
    return environment


def _dangerous_argument(value: str) -> bool:
    lowered = value.casefold()
    return (
        not value
        or "\x00" in value
        or any(marker in value for marker in ("&&", "||", ";", "`", "$(", "\n", "\r"))
        or lowered in {
            "install",
            "add",
            "exec",
            "dlx",
            "publish",
            "-c",
            "--command",
            "--eval",
            "--exec",
            "--script",
            "--with",
            "--from",
            "--require",
        }
        or lowered.startswith(
            ("--config=", "--plugin=", "--require=", "--with=", "--from=")
        )
    )


def _bounded_output(value: bytes) -> str:
    return value[:MAX_COMMAND_OUTPUT_BYTES].decode("utf-8", errors="replace")


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout: int,
    cancel_signal: CancellationSignal | None,
) -> tuple[bytes, bytes, str | None]:
    deadline = monotonic() + timeout
    while True:
        if cancel_signal is not None and cancel_signal.is_set():
            _terminate_process_tree(process)
            stdout, stderr = process.communicate()
            return stdout, stderr, "cancelled"
        remaining = deadline - monotonic()
        if remaining <= 0:
            _terminate_process_tree(process)
            stdout, stderr = process.communicate()
            return stdout, stderr, "timeout"
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return stdout, stderr, None
        except subprocess.TimeoutExpired:
            continue


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    else:
        process.kill()


def _tool_error(code: str, message: str):
    from leanharness.tools.contracts import ToolErrorInfo

    return ToolErrorInfo(code, message, True)
