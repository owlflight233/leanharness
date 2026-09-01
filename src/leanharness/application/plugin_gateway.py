"""Application services for selected local plugins."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from leanharness.permissions import PermissionMode
from leanharness.plugins.manager import PluginManager
from leanharness.storage import LocalStore, PluginRecord
from leanharness.tools import ToolRegistry


def plugin_to_dict(plugin: PluginRecord) -> dict[str, object]:
    return {
        "id": plugin.id,
        "name": plugin.name,
        "version": plugin.version,
        "description": plugin.description,
        "protocol_version": plugin.protocol_version,
        "enabled": plugin.enabled,
        "tools": list(plugin.tools),
        "installed_at": plugin.installed_at,
        "updated_at": plugin.updated_at,
    }


def plugin_registry_factory(
    store: LocalStore,
    workspace: Path,
    selected_plugin_ids: tuple[str, ...],
) -> Callable[..., ToolRegistry]:
    """Freeze the selected enabled tools for one runtime invocation."""

    tools = PluginManager(store).runtime_tools(workspace, selected_plugin_ids)

    def factory(path: Path, *, mode: PermissionMode = PermissionMode.INSPECT) -> ToolRegistry:
        return ToolRegistry(path, mode=mode, additional_tools=tools)

    return factory
