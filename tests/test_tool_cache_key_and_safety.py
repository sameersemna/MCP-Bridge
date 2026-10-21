"""Tool-result cache keys must cover every argument, and mutating tools must not
be cached or deduplicated (P0-05)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent

import mcp_bridge.openai_clients.utils as openai_utils
from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient
from mcp_bridge.mcp_clients.McpClientManager import ClientManager


def _result(text: str = "ok") -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


def _cache(tmp_path, ttl: int = 3600):
    return openai_utils.PersistentToolCache(
        cache_dir=str(tmp_path / "tool_cache"), ttl_seconds=ttl
    )


def test_differing_non_query_arguments_miss(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.put("search", {"query": "climate", "max_results": 5}, _result())

    assert cache.get("search", {"query": "climate", "max_results": 5}) is not None
    assert cache.get("search", {"query": "climate", "max_results": 10}) is None
    assert cache.get("search", {"query": "climate", "max_results": 5, "page": 2}) is None


def test_different_servers_do_not_share_entries(tmp_path) -> None:
    cache = _cache(tmp_path)
    arguments = {"query": "climate"}

    cache.put("search", arguments, _result(), server_name="server-a")

    assert cache.get("search", arguments, server_name="server-a") is not None
    assert cache.get("search", arguments, server_name="server-b") is None


def test_equivalent_json_hits_despite_key_order_and_whitespace(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.put("search", {"query": "a  b", "site": "example.com"}, _result())

    assert cache.get("search", {"site": "example.com", "query": "a b"}) is not None


def test_v1_cache_files_are_not_read(tmp_path) -> None:
    cache_dir = Path(tmp_path / "tool_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tool, query = "search", "climate"
    v1_key = hashlib.sha256(f"{tool}::{query}".encode()).hexdigest()
    v1_payload = {
        "tool_name": tool,
        "query": query,
        "cached_at": time.time(),
        "result": _result("stale v1").model_dump(),
    }
    (cache_dir / f"{v1_key}.json").write_text(json.dumps(v1_payload))

    cache = _cache(tmp_path)
    assert cache.get(tool, {"query": query}) is None


def test_payload_with_wrong_key_version_is_a_miss(tmp_path) -> None:
    cache = _cache(tmp_path)
    arguments = {"query": "climate"}
    key = openai_utils._tool_cache_key("", "search", arguments)
    payload = {
        "tool_name": "search",
        "query": "climate",
        "cached_at": time.time(),
        "key_version": 1,
        "result": _result("stale").model_dump(),
    }
    Path(tmp_path / "tool_cache" / f"{key}.json").write_text(json.dumps(payload))

    assert cache.get("search", arguments) is None


def test_fuzzy_cache_requires_identical_non_query_arguments() -> None:
    cache = openai_utils.ToolResultCache()
    result = {"isError": False}
    cache.put("search", "alpha beta gamma delta", result, other_arguments={"site": "a"})

    near = "alpha beta gamma delta epsilon"
    assert cache.get("search", near, other_arguments={"site": "a"}) is result
    assert cache.get("search", near, other_arguments={"site": "b"}) is None


def test_legacy_string_query_api_still_works(tmp_path) -> None:
    cache = _cache(tmp_path)
    cache.put("search", "plain query", _result())
    assert cache.get("search", "plain query") is not None


class _Client(GenericMcpClient):
    async def _maintain_session(self) -> None:
        return None


def _client_with(tool: str, **hints: object) -> _Client:
    client = _Client("srv")
    client.remember_tool_annotations(
        [SimpleNamespace(name=tool, model_extra={"annotations": hints})]
    )
    return client


def test_is_cache_safe_follows_annotations() -> None:
    assert _client_with("read", readOnlyHint=True).is_cache_safe("read") is True
    assert _client_with("write", readOnlyHint=False).is_cache_safe("write") is False
    assert _client_with("wipe", destructiveHint=True).is_cache_safe("wipe") is False
    # Unannotated keeps the legacy per-server opt-in behavior.
    assert _Client("srv").is_cache_safe("unknown") is True


def test_mutating_tool_is_never_cached(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    async def fake_call_tool(name: str, payload: str, timeout: float | None = None):
        calls.append(payload)
        return _result()

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)
    client = _client_with("write", readOnlyHint=False)
    monkeypatch.setattr(ClientManager, "clients", {"srv": client}, raising=False)

    cache = _cache(tmp_path)
    payload = json.dumps({"query": "x"})
    mapping = {"write": "srv"}

    asyncio.run(openai_utils.call_tools([("write", payload)], persistent_cache=cache, tool_server_map=mapping))
    asyncio.run(openai_utils.call_tools([("write", payload)], persistent_cache=cache, tool_server_map=mapping))

    assert len(calls) == 2, "a mutating tool was served from the cache"
    assert cache.get("write", {"query": "x"}, server_name="srv") is None


def test_read_only_tool_is_cached(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    async def fake_call_tool(name: str, payload: str, timeout: float | None = None):
        calls.append(payload)
        return _result()

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)
    client = _client_with("read", readOnlyHint=True)
    monkeypatch.setattr(ClientManager, "clients", {"srv": client}, raising=False)

    cache = _cache(tmp_path)
    payload = json.dumps({"query": "x"})
    mapping = {"read": "srv"}

    asyncio.run(openai_utils.call_tools([("read", payload)], persistent_cache=cache, tool_server_map=mapping))
    asyncio.run(openai_utils.call_tools([("read", payload)], persistent_cache=cache, tool_server_map=mapping))

    assert len(calls) == 1


def test_in_flight_dedupe_uses_all_arguments(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    async def slow_call(name: str, payload: str, timeout: float | None = None):
        calls.append(payload)
        await asyncio.sleep(0.05)
        return _result()

    monkeypatch.setattr(openai_utils, "call_tool", slow_call)
    client = _client_with("search", readOnlyHint=True)
    monkeypatch.setattr(ClientManager, "clients", {"srv": client}, raising=False)

    first = json.dumps({"query": "climate", "max_results": 1})
    second = json.dumps({"query": "climate", "max_results": 2})
    asyncio.run(
        openai_utils.call_tools(
            [("search", first), ("search", second)],
            tool_server_map={"search": "srv"},
        )
    )

    assert len(calls) == 2, "calls with different arguments were deduplicated"
