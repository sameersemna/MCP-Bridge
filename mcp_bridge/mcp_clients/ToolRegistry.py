"""Process-wide registry of tools discovered from downstream MCP servers.

Before this existed, every tool resolution and every request re-ran
``tools/list`` against every server. The registry is populated from one
discovery pass, refreshed on ``notifications/tools/list_changed`` or a TTL, and
resolved in config order so a name collision has a stable winner.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

DEFAULT_TOOL_REGISTRY_TTL_SECONDS = 300.0


def normalize_tool_name(tool: str) -> str:
    """Normalize separators/case the same way tool resolution always has."""
    return str(tool).strip().lower().replace("-", "_")


def _ttl_seconds() -> float:
    raw = os.getenv("MCP_BRIDGE_TOOL_REGISTRY_TTL_SECONDS")
    if raw is None:
        return DEFAULT_TOOL_REGISTRY_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            f"invalid MCP_BRIDGE_TOOL_REGISTRY_TTL_SECONDS value: {raw}; "
            f"using default {DEFAULT_TOOL_REGISTRY_TTL_SECONDS}"
        )
        return DEFAULT_TOOL_REGISTRY_TTL_SECONDS
    return value if value >= 0 else DEFAULT_TOOL_REGISTRY_TTL_SECONDS


@dataclass(frozen=True)
class RegisteredTool:
    server: str
    name: str
    tool: Any
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def schema(self) -> Any:
        return getattr(self.tool, "inputSchema", None)


class ToolRegistry:
    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else _ttl_seconds()
        )
        self._entries: dict[str, RegisteredTool] = {}
        self._order: list[RegisteredTool] = []
        self._server_entries: dict[str, list[RegisteredTool]] = {}
        self._server_order: list[str] = []
        self._server_refreshed: set[str] = set()
        self._refreshed_at: float | None = None
        self._collisions_logged: set[str] = set()
        self.lock = asyncio.Lock()

    def is_stale(self) -> bool:
        if self._refreshed_at is None:
            return True
        if self._ttl_seconds <= 0:
            return True
        return (time.monotonic() - self._refreshed_at) > self._ttl_seconds

    def is_server_stale(self, server: str) -> bool:
        """Whether this server has not been discovered in the current generation."""
        return server not in self._server_refreshed

    def set_server_order(self, servers: list[str]) -> None:
        self._server_order = list(servers)

    def server_order(self) -> list[str]:
        return list(self._server_order)

    def invalidate(self) -> None:
        """Drop the registry; the next resolution rediscovers the servers."""
        self._refreshed_at = None
        self._server_refreshed.clear()
        self._server_entries.clear()
        self._entries = {}
        self._order = []

    def merge_server(self, server: str, entries: list[RegisteredTool]) -> None:
        """Replace one server's entries, leaving other servers untouched."""
        if server not in self._server_order:
            self._server_order.append(server)
        self._server_entries[server] = list(entries)
        self._server_refreshed.add(server)
        self._rebuild()

    def replace(self, entries: list[RegisteredTool]) -> None:
        """Replace the whole registry from entries in config order."""
        grouped: dict[str, list[RegisteredTool]] = {}
        for entry in entries:
            grouped.setdefault(entry.server, []).append(entry)
        self._server_entries = grouped
        self._server_refreshed = set(grouped)
        for server in grouped:
            if server not in self._server_order:
                self._server_order.append(server)
        self._rebuild()
        self._refreshed_at = time.monotonic()

    def _rebuild(self) -> None:
        mapping: dict[str, RegisteredTool] = {}
        order: list[RegisteredTool] = []
        for server in self._server_order:
            for entry in self._server_entries.get(server, []):
                key = normalize_tool_name(entry.name)
                if not key:
                    continue
                existing = mapping.get(key)
                if existing is not None:
                    marker = f"{key}\x1f{existing.server}\x1f{entry.server}"
                    if marker not in self._collisions_logged:
                        self._collisions_logged.add(marker)
                        logger.warning(
                            f"tool name collision on '{entry.name}': keeping server "
                            f"'{existing.server}', ignoring '{entry.server}' "
                            "(earlier config entry wins)"
                        )
                    continue
                mapping[key] = entry
                order.append(entry)
        self._entries = mapping
        self._order = order

    def get(self, normalized_name: str) -> RegisteredTool | None:
        return self._entries.get(normalized_name)

    def all(self) -> list[RegisteredTool]:
        return list(self._order)

    def for_server(self, server: str) -> list[RegisteredTool]:
        return list(self._server_entries.get(server, []))


__all__ = [
    "DEFAULT_TOOL_REGISTRY_TTL_SECONDS",
    "RegisteredTool",
    "ToolRegistry",
    "normalize_tool_name",
]
