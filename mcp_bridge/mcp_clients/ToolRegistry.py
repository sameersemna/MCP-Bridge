"""Process-wide registry of tools discovered from downstream MCP servers.

Before this existed, every tool resolution and every request re-ran
``tools/list`` against every server. The registry is populated from one
discovery pass, refreshed on ``notifications/tools/list_changed`` or a TTL, and
resolved in config order so a name collision has a stable winner.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

DEFAULT_TOOL_REGISTRY_TTL_SECONDS = 300.0
DEFAULT_TOOL_NAMESPACING = "on_collision"
_MAX_OPENAI_NAME_LENGTH = 64
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def normalize_tool_name(tool: str) -> str:
    """Normalize separators/case the same way tool resolution always has."""
    return str(tool).strip().lower().replace("-", "_")


def sanitize_tool_name(name: str) -> str:
    """Make a name valid for an OpenAI function (``^[A-Za-z0-9_-]{1,64}$``)."""
    sanitized = _INVALID_NAME_CHARS.sub("_", str(name)).strip("_")
    return sanitized or "tool"


def _truncate_with_hash(name: str) -> str:
    """Truncate to 64 chars with a stable 8-char suffix so it stays unique."""
    if len(name) <= _MAX_OPENAI_NAME_LENGTH:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[:_MAX_OPENAI_NAME_LENGTH - 9]}_{digest}"


def make_exposed_name(server: str, tool: str, *, namespace: bool) -> str:
    """Return the model-facing name for a tool."""
    base = (
        f"{sanitize_tool_name(server)}__{sanitize_tool_name(tool)}"
        if namespace
        else sanitize_tool_name(tool)
    )
    return _truncate_with_hash(base)


def _configured_namespacing() -> str:
    try:
        from mcp_bridge.config import config

        value = getattr(config, "tool_namespacing", DEFAULT_TOOL_NAMESPACING)
        return value or DEFAULT_TOOL_NAMESPACING
    except Exception:
        return DEFAULT_TOOL_NAMESPACING


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
    # Model-facing name (sanitized/truncated, optionally server-prefixed).
    exposed: str = ""

    @property
    def schema(self) -> Any:
        return getattr(self.tool, "inputSchema", None)


class ToolRegistry:
    def __init__(
        self, ttl_seconds: float | None = None, namespacing: str | None = None
    ) -> None:
        self._ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else _ttl_seconds()
        )
        self._namespacing = namespacing or _configured_namespacing()
        self._entries: dict[str, RegisteredTool] = {}
        self._exposed: dict[str, RegisteredTool] = {}
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
        ordered: list[RegisteredTool] = []
        for server in self._server_order:
            ordered.extend(self._server_entries.get(server, []))

        groups: dict[str, list[RegisteredTool]] = {}
        for entry in ordered:
            groups.setdefault(sanitize_tool_name(entry.name), []).append(entry)

        exposed_map: dict[str, RegisteredTool] = {}
        exposed_order: list[RegisteredTool] = []
        canonical: dict[str, RegisteredTool] = {}
        updated_server_entries: dict[str, list[RegisteredTool]] = {}

        for entry in ordered:
            base = sanitize_tool_name(entry.name)
            collides = len({item.server for item in groups.get(base, [])}) > 1
            namespace = self._namespacing == "always" or (
                self._namespacing == "on_collision" and collides
            )
            exposed = make_exposed_name(entry.server, entry.name, namespace=namespace)
            exposed_entry = replace(entry, exposed=exposed)
            updated_server_entries.setdefault(entry.server, []).append(exposed_entry)

            canonical.setdefault(normalize_tool_name(entry.name), exposed_entry)

            key = normalize_tool_name(exposed)
            existing = exposed_map.get(key)
            if existing is not None:
                marker = f"{key}\x1f{existing.server}\x1f{entry.server}"
                if marker not in self._collisions_logged:
                    self._collisions_logged.add(marker)
                    logger.warning(
                        f"exposed tool name collision on '{exposed}': keeping "
                        f"server '{existing.server}', ignoring '{entry.server}'"
                    )
                continue
            exposed_map[key] = exposed_entry
            exposed_order.append(exposed_entry)

        self._entries = canonical
        self._exposed = exposed_map
        self._order = exposed_order
        self._server_entries = updated_server_entries

    def get(self, normalized_name: str) -> RegisteredTool | None:
        return self._exposed.get(normalized_name) or self._entries.get(normalized_name)

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
