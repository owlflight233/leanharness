"""Bounded JSONL process host for one plugin invocation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from leanharness.errors import PluginError

MAX_PLUGIN_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PLUGIN_STDERR_BYTES = 64 * 1024
MAX_PLUGIN_REQUEST_BYTES = 2 * 1024 * 1024
PLUGIN_TIMEOUT_SECONDS = 60


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class _BoundedReader:
    """Drain a child pipe without retaining more than the public limit."""

    def __init__(self, stream: Any, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self.data = bytearray()
        self.overflowed = False
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=1)

    def _read(self) -> None:
        while True:
            chunk = self._stream.read(64 * 1024)
            if not chunk:
                return
            remaining = self._limit - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.overflowed = True


class PluginHost:
    def __init__(self, root: Path, entrypoint: tuple[str, ...]) -> None:
        self.root = root.resolve(strict=True)
        self.entrypoint = entrypoint

    def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        artifact_dir: Path,
        cancel_signal: CancellationSignal | None = None,
    ) -> dict[str, Any]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, *self.entrypoint[1:]]
        safe_arguments = _normalize_json_unicode(arguments)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
        }
        # JSONL is always UTF-8, including non-BMP document text, regardless
        # of the host process console code page on Windows.
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["LEANHARNESS_PLUGIN_ARTIFACT_DIR"] = str(artifact_dir.resolve())
        requests = (
            {"protocol": "leanharness.plugin.v1", "type": "initialize"},
            {"protocol": "leanharness.plugin.v1", "type": "capabilities"},
            {
                "protocol": "leanharness.plugin.v1",
                "type": "tool.call",
                "tool": tool,
                "arguments": safe_arguments,
            },
            {"protocol": "leanharness.plugin.v1", "type": "shutdown"},
        )
        input_bytes = "".join(
            json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
            for request in requests
        ).encode("utf-8")
        if len(input_bytes) > MAX_PLUGIN_REQUEST_BYTES:
            raise PluginError("Plugin request exceeded the size limit")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=False,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_reader = _BoundedReader(process.stdout, MAX_PLUGIN_RESPONSE_BYTES)
            stderr_reader = _BoundedReader(process.stderr, MAX_PLUGIN_STDERR_BYTES)
            stdout_reader.start()
            stderr_reader.start()
            try:
                process.stdin.write(input_bytes)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                _terminate_process_tree(process)
                process.wait(timeout=2)
                stdout_reader.join()
                stderr_reader.join()
                raise PluginError("Plugin process closed its input") from exc
            deadline = monotonic() + PLUGIN_TIMEOUT_SECONDS
            stop_reason: str | None = None
            while process.poll() is None:
                if cancel_signal is not None and cancel_signal.is_set():
                    stop_reason = "cancelled"
                    _terminate_process_tree(process)
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    stop_reason = "timeout"
                    _terminate_process_tree(process)
                    break
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                process.wait(timeout=2)
            stdout_reader.join()
            stderr_reader.join()
        except OSError as exc:
            raise PluginError("Plugin process could not start") from exc
        if stop_reason == "cancelled":
            raise PluginError("Plugin call was cancelled")
        if stop_reason == "timeout":
            raise PluginError("Plugin call timed out")
        if stdout_reader.overflowed or stderr_reader.overflowed:
            raise PluginError("Plugin response exceeded the size limit")
        if process.returncode != 0:
            raise PluginError("Plugin process failed")
        stdout = bytes(stdout_reader.data).decode("utf-8", errors="replace")
        stderr = bytes(stderr_reader.data).decode("utf-8", errors="replace")
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 4:
            raise PluginError("Plugin returned an invalid response")
        try:
            responses = [json.loads(line) for line in lines]
        except json.JSONDecodeError as exc:
            raise PluginError("Plugin returned malformed JSON") from exc
        expected_types = (
            "initialized",
            "capabilities.result",
            "tool.result",
            "shutdown.complete",
        )
        for response, expected in zip(responses, expected_types, strict=True):
            if (
                (
                    not isinstance(response, dict)
                    or response.get("protocol") != "leanharness.plugin.v1"
                    or response.get("type") != expected
                    or response.get("ok") is not True
                )
                and (
                    expected != "tool.result"
                    or not (
                        isinstance(response, dict)
                        and response.get("protocol") == "leanharness.plugin.v1"
                        and response.get("type") == expected
                        and response.get("ok") is False
                    )
                )
            ):
                raise PluginError("Plugin response did not match the protocol")
        response = responses[2]
        if (
            not isinstance(response, dict)
            or response.get("protocol") != "leanharness.plugin.v1"
            or response.get("type") != "tool.result"
            or not isinstance(response.get("ok"), bool)
        ):
            raise PluginError("Plugin response did not match the protocol")
        if stderr:
            # stderr is deliberately ignored; it is never exposed as tool data.
            _ = stderr[:4096]
        return response


def _normalize_json_unicode(value: Any) -> Any:
    """Convert valid JSON surrogate pairs to Unicode scalars before UTF-8 transport."""

    if isinstance(value, str):
        try:
            return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise PluginError("Plugin request contains invalid Unicode") from exc
    if isinstance(value, list):
        return [_normalize_json_unicode(item) for item in value]
    if isinstance(value, dict):
        return {
            _normalize_json_unicode(key): _normalize_json_unicode(item)
            for key, item in value.items()
        }
    return value


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
