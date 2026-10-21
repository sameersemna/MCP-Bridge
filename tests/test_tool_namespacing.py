"""Tool names exposed to clients must be unique and OpenAI-compatible (P1-02)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mcp_bridge.config.final import Settings
from mcp_bridge.mcp_clients.McpClientManager import MCPClientManager
from mcp_bridge.mcp_clients.ToolRegistry import (
    RegisteredTool,
    ToolRegistry,
    make_exposed_name,
    normalize_tool_name,
    sanitize_tool_name,
)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = None
        self.inputSchema = {"type": "object"}


class _Session:
    def __init__(self, tools: list[_Tool]) -> None:
        self.tools = tools

    async def list_tools(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(tools=self.tools)


class _Client:
    def __init__(self, name: str, tools: list[_Tool]) -> None:
        self.name = name
        self.session = _Session(tools)
        self._tool_annotations: dict[str, object] = {}


def _entry(server: str, name: str) -> RegisteredTool:
    return RegisteredTool(server=server, name=name, tool=_Tool(name))


def test_on_collision_namespaces_only_duplicate_names() -> None:
    registry = ToolRegistry(ttl_seconds=300, namespacing="on_collision")
    registry.set_server_order(["a", "b"])
    registry.merge_server("a", [_entry("a", "search"), _entry("a", "unique")])
    registry.merge_server("b", [_entry("b", "search")])

    exposed = {entry.exposed: entry for entry in registry.all()}

    assert set(exposed) == {"a__search", "b__search", "unique"}
    assert exposed["a__search"].server == "a"
    assert exposed["b__search"].server == "b"


def test_always_namespaces_every_tool() -> None:
    registry = ToolRegistry(ttl_seconds=300, namespacing="always")
    registry.merge_server("srv", [_entry("srv", "unique")])

    assert registry.all()[0].exposed == "srv__unique"


def test_names_are_sanitized_and_truncated_to_64_chars() -> None:
    registry = ToolRegistry(ttl_seconds=300, namespacing="none")
    registry.merge_server(
        "srv", [_entry("srv", "a.b/c"), _entry("srv", "x" * 100)]
    )

    names = [entry.exposed for entry in registry.all()]

    assert "a_b_c" in names
    long_name = next(name for name in names if len(name) == 64)
    assert long_name[-9] == "_"
    assert len(long_name.rsplit("_", 1)[1]) == 8
    # Stable: the same input produces the same truncated name.
    assert make_exposed_name("srv", "x" * 100, namespace=False) == long_name
    assert sanitize_tool_name("a.b/c") == "a_b_c"


def test_namespaced_name_resolves_back_to_canonical_tool() -> None:
    first = _Client("a", [_Tool("search")])
    second = _Client("b", [_Tool("search")])
    manager = MCPClientManager()
    manager.clients = {"a": first, "b": second}
    manager.registry = ToolRegistry(ttl_seconds=300, namespacing="on_collision")

    async def run():  # type: ignore[no-untyped-def]
        await manager.refresh_tool_registry(force=True)
        one = await manager.resolve_tool("a__search")
        two = await manager.resolve_tool("b__search")
        return one, two

    one, two = asyncio.run(run())

    assert one is not None and one[0] is first and one[1] == "search"
    assert two is not None and two[0] is second and two[1] == "search"


def test_registry_get_accepts_the_exposed_name() -> None:
    registry = ToolRegistry(ttl_seconds=300, namespacing="always")
    registry.merge_server("srv", [_entry("srv", "search")])

    assert registry.get(normalize_tool_name("srv__search")) is not None
    assert registry.get(normalize_tool_name("search")) is not None


def test_settings_accepts_the_namespacing_policy() -> None:
    assert Settings(tool_namespacing="always").tool_namespacing == "always"
    assert Settings().tool_namespacing == "on_collision"
