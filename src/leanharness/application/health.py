"""Read-only application health projection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from leanharness import __version__
from leanharness.config import AppConfig


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Public process state safe to return to the local frontend."""

    status: Literal["ok"]
    name: str
    version: str
    workspace: str
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        return payload


def get_health(config: AppConfig) -> HealthSnapshot:
    """Build the foundation health response without probing external services."""

    return HealthSnapshot(
        status="ok",
        name="LeanHarness",
        version=__version__,
        workspace=str(config.workspace),
        capabilities=(
            "model.chat",
            "model.streaming",
            "agent.inspect",
            "agent.streaming",
            "agent.delegation",
            "session.persistence",
            "run.trace",
            "agent.edit",
            "tool.mkdir",
            "tool.patch",
            "tool.command",
            "tool.git.read",
            "approval.interactive",
            "input.interactive",
            "input.attachment",
            "plugin.local",
            "tool.docx",
        ),
    )
