import asyncio

import pytest

from mcp_bridge.openai_clients.utils import (
    _extract_fetch_url,
    _fetch_via_wayback,
    _is_fetch_blocked_error,
    _is_fetch_tool,
    get_fetch_mirror_fallback_enabled,
)


def test_is_fetch_tool_matches_fetch_variants():
    assert _is_fetch_tool("fetch") is True
    assert _is_fetch_tool("fetch_content") is True
    assert _is_fetch_tool("web_fetch") is True
    assert _is_fetch_tool("search") is False
    assert _is_fetch_tool(None) is False


def test_is_fetch_blocked_error_detects_robots_txt():
    assert _is_fetch_blocked_error("The sites robots.txt specifies that autonomous fetching is not allowed") is True
    assert _is_fetch_blocked_error("Failed to fetch - status code 403") is True
    assert _is_fetch_blocked_error("Cloudflare bot protection blocked the request") is True
    assert _is_fetch_blocked_error("Checking your browser...") is True


def test_is_fetch_blocked_error_ignores_benign_errors():
    assert _is_fetch_blocked_error("Timeout Error calling fetch") is False
    assert _is_fetch_blocked_error("Connection refused") is False
    assert _is_fetch_blocked_error(None) is False
    assert _is_fetch_blocked_error("") is False


def test_extract_fetch_url():
    assert _extract_fetch_url({"url": "https://example.com"}) == "https://example.com"
    assert _extract_fetch_url({"url": "  https://example.com  "}) == "https://example.com"
    assert _extract_fetch_url({"url": ""}) is None
    assert _extract_fetch_url({}) is None
    assert _extract_fetch_url(None) is None


def test_get_fetch_mirror_fallback_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("MCP_BRIDGE_FETCH_MIRROR_FALLBACK", raising=False)
    assert get_fetch_mirror_fallback_enabled() is True


def test_get_fetch_mirror_fallback_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_FETCH_MIRROR_FALLBACK", "false")
    assert get_fetch_mirror_fallback_enabled() is False
    monkeypatch.setenv("MCP_BRIDGE_FETCH_MIRROR_FALLBACK", "1")
    assert get_fetch_mirror_fallback_enabled() is True


def test_fetch_via_wayback_returns_none_when_no_snapshot(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"archived_snapshots": {}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("mcp_bridge.openai_clients.utils.httpx.AsyncClient", FakeClient)

    result = asyncio.run(_fetch_via_wayback("https://example.com"))
    assert result is None


def test_fetch_via_wayback_returns_content_when_snapshot_exists(monkeypatch):
    class FakeAvailabilityResponse:
        status_code = 200

        def json(self):
            return {
                "archived_snapshots": {
                    "closest": {"url": "https://web.archive.org/web/20240101/https://example.com"}
                }
            }

    class FakeContentResponse:
        status_code = 200
        text = "Archived page content here"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            if "wayback/available" in url:
                return FakeAvailabilityResponse()
            return FakeContentResponse()

    monkeypatch.setattr("mcp_bridge.openai_clients.utils.httpx.AsyncClient", FakeClient)

    result = asyncio.run(_fetch_via_wayback("https://example.com"))
    assert result is not None
    assert result.isError is False
    assert "Wayback Machine" in result.content[0].text
    assert "Archived page content here" in result.content[0].text
