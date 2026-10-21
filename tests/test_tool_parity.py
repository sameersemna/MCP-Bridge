"""Streaming and non-streaming agree on client vs MCP tool handling (P1-09)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mcp_bridge.openai_clients.utils as openai_utils
from mcp_bridge.mcp_clients.ToolRegistry import RegisteredTool
from mcp_bridge.openai_clients.tool_policy import split_tool_calls


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = None
        self.inputSchema = {"type": "object"}


def test_client_tools_are_merged_and_win_name_clashes(monkeypatch) -> None:
    async def noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(
        openai_utils, "_ensure_client_manager_initialized", noop, raising=False
    )
    monkeypatch.setattr(
        openai_utils.ClientManager, "refresh_tool_registry", noop, raising=False
    )
    entries = [
        RegisteredTool(server="srv", name="search", tool=_Tool("search"), exposed="search"),
        RegisteredTool(server="srv", name="my_tool", tool=_Tool("my_tool"), exposed="my_tool"),
    ]
    monkeypatch.setattr(
        openai_utils.ClientManager,
        "registry",
        SimpleNamespace(all=lambda: entries),
        raising=False,
    )

    client_tool = SimpleNamespace(
        function=SimpleNamespace(name="my_tool")
    )
    request = SimpleNamespace(tools=[client_tool], messages=[])

    result = asyncio.run(openai_utils.chat_completion_add_tools(request))

    names = [tool.function.name for tool in result.tools]
    assert names == ["my_tool", "search"]
    assert result._client_tool_names == {"my_tool"}


def test_split_client_only() -> None:
    request = SimpleNamespace(_client_tool_names={"client_tool"})

    client_calls, mcp_calls = split_tool_calls(
        request, [("client_tool", "{}"), ("client_tool", '{"a":1}')]
    )

    assert len(client_calls) == 2
    assert mcp_calls == []


def test_split_mcp_only() -> None:
    request = SimpleNamespace(_client_tool_names={"client_tool"})

    client_calls, mcp_calls = split_tool_calls(request, [("search", "{}")])

    assert client_calls == []
    assert mcp_calls == [("search", "{}")]


def test_split_mixed() -> None:
    request = SimpleNamespace(_client_tool_names={"client_tool"})

    client_calls, mcp_calls = split_tool_calls(
        request, [("client_tool", "{}"), ("search", "{}")]
    )

    assert client_calls == [("client_tool", "{}")]
    assert mcp_calls == [("search", "{}")]