"""Local-only plugin installation and discovery."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from leanharness.errors import PluginError, PluginManifestError, PluginNotFoundError
from leanharness.plugins.contracts import PluginManifest, parse_manifest
from leanharness.storage import LocalStore, PluginRecord
from leanharness.tools.contracts import BuiltinTool
from leanharness.tools.workspace import WorkspaceBoundary

MAX_PLUGIN_FILES = 100
MAX_PLUGIN_INSTALL_BYTES = 20 * 1024 * 1024


class PluginManager:
    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self.root = store.data_dir / "plugins"
        self.root.mkdir(parents=True, exist_ok=True)

    def install(self, source: str | Path) -> PluginRecord:
        source_path = Path(source).expanduser().resolve(strict=True)
        if not source_path.is_dir():
            raise PluginError("Plugin source must be a directory")
        manifest = self._read_manifest(source_path)
        self._validate_source(source_path, manifest)
        target = self.root / manifest.id
        if target.exists():
            try:
                self.store.get_plugin(manifest.id)
            except PluginNotFoundError:
                # A prior install may have copied files before its metadata
                # transaction failed. Recover only this exact, unreferenced
                # plugin directory so the next install is deterministic.
                if target.is_symlink() or not target.is_dir():
                    raise PluginError("Plugin install path is invalid") from None
                shutil.rmtree(target)
            else:
                raise PluginError("Plugin is already installed; remove it before reinstalling")
        try:
            shutil.copytree(source_path, target)
        except OSError as exc:
            raise PluginError("Plugin files could not be installed") from exc
        try:
            return self.store.save_plugin(manifest, source_path=source_path, install_path=target)
        except Exception:
            # Do not leave an unreferenced executable plugin tree if the
            # metadata transaction fails.
            shutil.rmtree(target, ignore_errors=True)
            raise

    def list(self) -> list[PluginRecord]:
        return self.store.list_plugins()

    def enable(self, plugin_id: str) -> PluginRecord:
        return self.store.set_plugin_enabled(plugin_id, True)

    def disable(self, plugin_id: str) -> PluginRecord:
        return self.store.set_plugin_enabled(plugin_id, False)

    def remove(self, plugin_id: str) -> None:
        self.store.delete_plugin(plugin_id)

    def enabled_manifests(self) -> tuple[tuple[PluginRecord, PluginManifest, Path], ...]:
        result: list[tuple[PluginRecord, PluginManifest, Path]] = []
        for record in self.store.list_plugins():
            if not record.enabled:
                continue
            root = Path(record.install_path).resolve(strict=True)
            manifest = self._read_manifest(root)
            result.append((record, manifest, root))
        return tuple(result)

    def runtime_tools(
        self, workspace: Path, selected_ids: tuple[str, ...]
    ) -> tuple[BuiltinTool, ...]:
        if len(set(selected_ids)) != len(selected_ids):
            raise PluginError("Selected plugin IDs must be unique")
        enabled = {
            record.id: (record, manifest, root)
            for record, manifest, root in self.enabled_manifests()
        }
        missing = [plugin_id for plugin_id in selected_ids if plugin_id not in enabled]
        if missing:
            raise PluginError("A selected plugin is missing or disabled")
        from leanharness.plugins.tool import PluginTool

        boundary = WorkspaceBoundary.create(workspace)
        artifact_root = self.store.data_dir / "plugin-artifacts"
        tools: list[BuiltinTool] = []
        for plugin_id in selected_ids:
            _, manifest, root = enabled[plugin_id]
            tools.extend(
                PluginTool(root, manifest, tool, boundary, artifact_root)
                for tool in manifest.tools
            )
        return tuple(tools)

    @staticmethod
    def _read_manifest(root: Path) -> PluginManifest:
        path = root / "leanharness-plugin.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginManifestError("Plugin manifest could not be read") from exc
        return parse_manifest(value)

    @staticmethod
    def _validate_source(root: Path, manifest: PluginManifest) -> None:
        files = 0
        total = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                raise PluginError("Plugin source cannot contain symbolic links")
            if path.is_file():
                files += 1
                total += path.stat().st_size
                if files > MAX_PLUGIN_FILES or total > MAX_PLUGIN_INSTALL_BYTES:
                    raise PluginError("Plugin source exceeds the installation limit")
        script = root / manifest.entrypoint[1] if len(manifest.entrypoint) > 1 else None
        if script is None or not script.is_file() or script.suffix.casefold() != ".py":
            raise PluginError("Plugin Python entrypoint is missing")
