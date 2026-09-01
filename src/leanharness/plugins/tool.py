"""Tool Registry adapter for an enabled out-of-process plugin."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from leanharness.errors import PluginError
from leanharness.plugins.contracts import PluginManifest, PluginToolManifest
from leanharness.plugins.host import CancellationSignal, PluginHost
from leanharness.tools.contracts import ToolExecutionError, ToolResult
from leanharness.tools.workspace import WorkspaceBoundary, _reject_unknown

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class PluginTool:
    supports_cancellation = True

    def __init__(
        self,
        plugin_root: Path,
        manifest: PluginManifest,
        tool: PluginToolManifest,
        boundary: WorkspaceBoundary,
        artifact_root: Path,
    ) -> None:
        self.plugin_root = plugin_root
        self.manifest = manifest
        self.plugin = tool
        self.boundary = boundary
        self.artifact_root = artifact_root
        self.definition = tool.definition
        self.host = PluginHost(plugin_root, manifest.entrypoint)

    @property
    def is_mutating(self) -> bool:
        return self.plugin.mutation

    def preview(self, arguments: dict[str, Any]) -> dict[str, object]:
        self._validate(arguments)
        output_path = self._output_path(arguments)
        return {
            "plugin_id": self.manifest.id,
            "plugin_version": self.manifest.version,
            "tool": self.definition.name,
            "path": output_path,
            "artifact_extension": self.plugin.artifact_extension,
        }

    def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
        *,
        cancel_signal: CancellationSignal | None = None,
    ) -> ToolResult:
        self._validate(arguments)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.artifact_root) as temporary:
            run_dir = Path(temporary)
            metadata = self._generate_artifact(arguments, run_dir, cancel_signal)
        return ToolResult(
            tool_call_id,
            self.definition.name,
            True,
            metadata,
            public_metadata=metadata,
        )

    def _generate_artifact(
        self,
        arguments: dict[str, Any],
        run_dir: Path,
        cancel_signal: CancellationSignal | None,
    ) -> dict[str, object]:
        try:
            response = self.host.call(
                self.definition.name, arguments, run_dir, cancel_signal
            )
        except PluginError as exc:
            raise ToolExecutionError(exc.code, exc.message, recoverable=True) from exc
        if not response.get("ok"):
            error = response.get("error")
            code = (
                str(error.get("code", "PLUGIN_TOOL_FAILED"))
                if isinstance(error, dict)
                else "PLUGIN_TOOL_FAILED"
            )
            message = (
                str(error.get("message", "Plugin tool failed"))
                if isinstance(error, dict)
                else "Plugin tool failed"
            )
            raise ToolExecutionError(code, message)
        artifact = response.get("artifact")
        if not isinstance(artifact, dict):
            raise ToolExecutionError(
                "PLUGIN_ARTIFACT_MISSING", "Plugin did not return an artifact"
            )
        source_name = artifact.get("filename")
        if not isinstance(source_name, str) or Path(source_name).name != source_name:
            raise ToolExecutionError(
                "PLUGIN_ARTIFACT_INVALID", "Plugin artifact filename is invalid"
            )
        source = (run_dir / source_name).resolve()
        if (
            not source.is_relative_to(run_dir.resolve())
            or not source.is_file()
            or source.is_symlink()
        ):
            raise ToolExecutionError(
                "PLUGIN_ARTIFACT_INVALID", "Plugin artifact is outside its directory"
            )
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ToolExecutionError(
                "PLUGIN_ARTIFACT_TOO_LARGE", "Plugin artifact exceeds 8 MiB"
            )
        if artifact.get("sha256") != digest or artifact.get("byte_size") != len(content):
            raise ToolExecutionError(
                "PLUGIN_ARTIFACT_INVALID", "Plugin artifact metadata is invalid"
            )
        if artifact.get("media_type") != self.plugin.artifact_media_type or not content.startswith(
            b"PK"
        ):
            raise ToolExecutionError("PLUGIN_ARTIFACT_INVALID", "Plugin artifact type is invalid")
        try:
            with zipfile.ZipFile(source) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise ValueError("DOCX package parts are missing")
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            raise ToolExecutionError(
                "PLUGIN_ARTIFACT_INVALID", "Plugin artifact is not a valid DOCX package"
            ) from exc
        destination = self.boundary.resolve_output(
            self._output_path(arguments), require_parent=False
        )[0]
        if destination.exists():
            raise ToolExecutionError(
                "PATH_ALREADY_EXISTS", "Plugin artifact target already exists"
            )
        if destination.suffix.casefold() != self.plugin.artifact_extension:
            raise ToolExecutionError("PLUGIN_ARTIFACT_INVALID", "Artifact extension is not allowed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".leanharness-{destination.name}.tmp")
        try:
            shutil.copyfile(source, temp)
            # Hard-link creation is atomic and fails instead of overwriting a
            # destination created concurrently after the initial existence check.
            os.link(temp, destination)
            temp.unlink(missing_ok=True)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            if destination.exists():
                raise ToolExecutionError(
                    "PATH_ALREADY_EXISTS", "Plugin artifact target already exists"
                ) from exc
            raise ToolExecutionError(
                "PLUGIN_ARTIFACT_WRITE_FAILED",
                "Artifact could not be stored",
                recoverable=False,
            ) from exc
        return {
            "plugin_id": self.manifest.id,
            "plugin_version": self.manifest.version,
            "path": destination.relative_to(self.boundary.root).as_posix(),
            "bytes": len(content),
            "sha256": digest,
            "media_type": self.plugin.artifact_media_type,
        }

    def _validate(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolExecutionError(
                "TOOL_INVALID_ARGUMENTS", "Plugin arguments must be an object"
            )
        _reject_unknown(arguments, set(self.plugin.parameters.get("properties", {})))

    def _output_path(self, arguments: dict[str, Any]) -> str:
        value = arguments.get("output_path", arguments.get("filename", "document.docx"))
        if (
            not isinstance(value, str)
            or not value
            or Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            raise ToolExecutionError(
                "PATH_OUTSIDE_WORKSPACE", "Plugin output path must be relative"
            )
        return value.replace("\\", "/")
