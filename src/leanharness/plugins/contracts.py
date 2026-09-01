"""Strict contracts for LeanHarness local process plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanharness.errors import PluginManifestError
from leanharness.models import ToolDefinition

PLUGIN_PROTOCOL = "leanharness.plugin.v1"
MAX_PLUGIN_TOOLS = 8
MAX_PLUGIN_ID_CHARS = 48
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


@dataclass(frozen=True, slots=True)
class PluginToolManifest:
    name: str
    description: str
    parameters: dict[str, Any]
    mutation: bool
    artifact_extension: str | None = None
    artifact_media_type: str | None = None

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(self.name, self.description, self.parameters)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "mutation": self.mutation,
            "artifact_extension": self.artifact_extension,
            "artifact_media_type": self.artifact_media_type,
        }


@dataclass(frozen=True, slots=True)
class PluginManifest:
    id: str
    name: str
    version: str
    description: str
    protocol_version: str
    entrypoint: tuple[str, ...]
    tools: tuple[PluginToolManifest, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "protocol_version": self.protocol_version,
            "entrypoint": list(self.entrypoint),
            "tools": [tool.to_dict() for tool in self.tools],
        }


@dataclass(frozen=True, slots=True)
class PluginArtifact:
    temporary_path: Path
    filename: str
    media_type: str
    byte_size: int
    sha256: str


def parse_manifest(value: object) -> PluginManifest:
    if not isinstance(value, dict):
        raise PluginManifestError("Plugin manifest must be a JSON object")
    allowed = {
        "id", "name", "version", "description", "protocol_version", "entrypoint", "tools"
    }
    if set(value) - allowed:
        raise PluginManifestError("Plugin manifest contains unknown fields")
    plugin_id = _required_text(value, "id", MAX_PLUGIN_ID_CHARS)
    if not _IDENTIFIER.fullmatch(plugin_id):
        raise PluginManifestError("Plugin id must be a lowercase hyphenated identifier")
    name = _required_text(value, "name", 80)
    version = _required_text(value, "version", 32)
    description = _required_text(value, "description", 500)
    protocol = _required_text(value, "protocol_version", 64)
    if protocol != PLUGIN_PROTOCOL:
        raise PluginManifestError("Plugin protocol version is unsupported")
    raw_entrypoint = value.get("entrypoint")
    if (
        not isinstance(raw_entrypoint, list)
        or not 1 <= len(raw_entrypoint) <= 8
        or not all(isinstance(item, str) and item and len(item) <= 200 for item in raw_entrypoint)
    ):
        raise PluginManifestError("Plugin entrypoint must be a bounded string array")
    entrypoint = tuple(raw_entrypoint)
    if entrypoint[0] not in {"python"}:
        raise PluginManifestError("Plugin entrypoint must use the bundled Python runtime")
    for argument in entrypoint[1:]:
        candidate = Path(argument)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise PluginManifestError("Plugin entrypoint paths must stay inside the plugin")
    raw_tools = value.get("tools")
    if not isinstance(raw_tools, list) or not 1 <= len(raw_tools) <= MAX_PLUGIN_TOOLS:
        raise PluginManifestError("Plugin must declare between 1 and 8 tools")
    tools = tuple(_parse_tool(item) for item in raw_tools)
    if len({tool.name for tool in tools}) != len(tools):
        raise PluginManifestError("Plugin tool names must be unique")
    return PluginManifest(
        plugin_id, name, version, description, protocol, entrypoint, tools
    )


def _parse_tool(value: object) -> PluginToolManifest:
    if not isinstance(value, dict):
        raise PluginManifestError("Plugin tool declaration must be an object")
    allowed = {
        "name", "description", "parameters", "mutation", "artifact_extension",
        "artifact_media_type",
    }
    if set(value) - allowed:
        raise PluginManifestError("Plugin tool contains unknown fields")
    name = _required_text(value, "name", 64)
    if not _TOOL_NAME.fullmatch(name):
        raise PluginManifestError("Plugin tool name is invalid")
    description = _required_text(value, "description", 600)
    parameters = value.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise PluginManifestError("Plugin tool parameters must be a JSON object schema")
    if parameters.get("additionalProperties") is not False:
        raise PluginManifestError("Plugin tool schema must reject unknown properties")
    mutation = value.get("mutation")
    if not isinstance(mutation, bool):
        raise PluginManifestError("Plugin tool mutation flag is required")
    extension = value.get("artifact_extension")
    media_type = value.get("artifact_media_type")
    if mutation:
        if not isinstance(extension, str) or not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            raise PluginManifestError("A mutating plugin tool requires an artifact extension")
        if not isinstance(media_type, str) or not media_type:
            raise PluginManifestError("A mutating plugin tool requires an artifact media type")
    elif extension is not None or media_type is not None:
        raise PluginManifestError("Read-only plugin tools cannot declare artifacts")
    return PluginToolManifest(name, description, parameters, mutation, extension, media_type)


def _required_text(value: dict[str, Any], key: str, maximum: int) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip() or len(item) > maximum:
        raise PluginManifestError(f"Plugin manifest field {key} is invalid")
    return item.strip()
