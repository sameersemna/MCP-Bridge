"""HTTP-hosted config loading must be bounded and fail loudly (P0-12).

The old loader used an implicit timeout, never checked the status, and returned
{} on a 404 or a parse error, so the bridge started with no servers and no clear
cause.
"""

from __future__ import annotations

import httpx
import pytest

import mcp_bridge.config.http as config_http
from mcp_bridge.config.http import load_config

_URL = "https://example.com/config.json"


def _mock(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def test_successful_json_object_is_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"mcp_servers": {}}, request=request
        )

    assert load_config(_URL, transport=_mock(handler)) == {"mcp_servers": {}}


def test_http_error_exits_non_zero(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found", request=request)

    with pytest.raises(SystemExit) as exc:
        load_config(_URL, transport=_mock(handler))
    assert exc.value.code == 1


def test_timeout_exits_non_zero(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(SystemExit):
        load_config(_URL, transport=_mock(handler))


def test_non_json_content_type_exits(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>login</html>",
            headers={"content-type": "text/html"},
            request=request,
        )

    with pytest.raises(SystemExit):
        load_config(_URL, transport=_mock(handler))


def test_invalid_json_exits(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{not json",
            headers={"content-type": "application/json"},
            request=request,
        )

    with pytest.raises(SystemExit):
        load_config(_URL, transport=_mock(handler))


def test_non_object_json_exits(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3], request=request)

    with pytest.raises(SystemExit):
        load_config(_URL, transport=_mock(handler))


def test_optional_mode_downgrades_failure_to_empty_config(monkeypatch) -> None:
    monkeypatch.setenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", "true")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", request=request)

    assert load_config(_URL, transport=_mock(handler)) == {}


def test_timeout_environment_variable_is_applied(monkeypatch) -> None:
    monkeypatch.setenv("MCP_BRIDGE_CONFIG_HTTP_TIMEOUT_SECONDS", "5")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={}, request=request)

    load_config(_URL, transport=_mock(handler))

    timeout = seen["timeout"]
    assert isinstance(timeout, dict)
    assert timeout["read"] == 5.0


def test_query_string_is_not_written_to_the_log(monkeypatch) -> None:
    monkeypatch.delenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", raising=False)
    messages: list[str] = []
    monkeypatch.setattr(
        config_http.logger, "error", lambda message: messages.append(message)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="denied", request=request)

    with pytest.raises(SystemExit):
        load_config(f"{_URL}?token=super-secret-token", transport=_mock(handler))

    assert messages
    assert all("super-secret-token" not in message for message in messages)
    assert any("example.com" in message for message in messages)
