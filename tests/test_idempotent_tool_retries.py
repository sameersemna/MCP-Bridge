"""Timed-out tool calls may only be retried when declared safe (P0-04).

A timeout does not mean the server did not run the tool. Retrying an
unannotated mutating tool can duplicate a write. These tests pin the rule:
read-only/idempotent (or explicitly allow-listed) tools retry; everything else
returns an "outcome unknown, not retried" error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent

import mcp_bridge.mcp_clients.AbstractClient as abstract_client_module
from mcp_bridge.config import config as app_config
from mcp_bridge.config.final import Settings
from mcp_bridge.mcp_clients.AbstractClient import (
    DEFAULT_MCP_TOOL_RETRY_COUNT,
    GenericMcpClient,
)

ROOT = Path(__file__).resolve().parents[1]


class _FlakySession:
    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise asyncio.TimeoutError()
        return CallToolResult(
            content=[TextContent(type="text", text="ok")], isError=False
        )


class _Client(GenericMcpClient):
    """A client whose session times out on the first ``fail_times`` calls."""

    def __init__(self, fail_times: int = 1) -> None:
        super().__init__("dummy")
        self._session = _FlakySession(fail_times)
        self.session = self._session

    async def _maintain_session(self) -> None:
        return None

    async def _wait_for_session(
        self,
        timeout: int | None = None,
        http_error: bool = True,
        log_interval: float | None = None,
        poll_interval: float | None = None,
    ) -> None:
        if self.session is None:
            self.session = self._session


def _annotate(client: _Client, tool: str, **hints: object) -> None:
    client.remember_tool_annotations(
        [SimpleNamespace(name=tool, model_extra={"annotations": hints})]
    )


def _call(monkeypatch, client: _Client, tool: str, retries: int = 1) -> CallToolResult:
    # Deterministic retry behaviour: never read the ambient env, never sleep.
    monkeypatch.setattr(
        abstract_client_module, "get_tool_retry_count", lambda: retries
    )
    monkeypatch.setattr(
        abstract_client_module, "get_tool_retry_delay_seconds", lambda: 0.0
    )
    return asyncio.run(client.call_tool(tool, {}, timeout=1))


def _text(result: CallToolResult) -> str:
    return result.content[0].text


def test_read_only_tool_is_retried(monkeypatch) -> None:
    client = _Client(fail_times=1)
    _annotate(client, "fetch", readOnlyHint=True)

    result = _call(monkeypatch, client, "fetch")

    assert result.isError is False
    assert client._session.attempts == 2


def test_idempotent_tool_is_retried(monkeypatch) -> None:
    client = _Client(fail_times=1)
    _annotate(client, "status", idempotentHint=True)

    result = _call(monkeypatch, client, "status")

    assert result.isError is False
    assert client._session.attempts == 2


def test_destructive_tool_is_not_retried(monkeypatch) -> None:
    client = _Client(fail_times=1)
    _annotate(client, "write_file", destructiveHint=True)

    result = _call(monkeypatch, client, "write_file")

    assert result.isError is True
    assert client._session.attempts == 1
    assert "not retried" in _text(result).lower()


def test_unannotated_tool_is_not_retried_and_reports_unknown_outcome(monkeypatch) -> None:
    client = _Client(fail_times=1)

    result = _call(monkeypatch, client, "send_message")

    assert result.isError is True
    assert client._session.attempts == 1
    message = _text(result).lower()
    assert "unknown" in message
    assert "not retried" in message


def test_allow_listed_tool_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(
        app_config,
        "idempotent_tools_by_server",
        {"dummy": {"write_file"}},
        raising=False,
    )
    client = _Client(fail_times=1)

    result = _call(monkeypatch, client, "write_file")

    assert result.isError is False
    assert client._session.attempts == 2


def test_retry_unsafe_server_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(
        app_config, "retry_unsafe_servers", {"dummy"}, raising=False
    )
    client = _Client(fail_times=1)

    result = _call(monkeypatch, client, "send_message")

    assert result.isError is False
    assert client._session.attempts == 2


def test_config_captures_and_strips_the_new_per_server_keys() -> None:
    settings = Settings(
        mcp_servers={
            "remote": {
                "type": "sse",
                "url": "http://localhost:11403/sse",
                "idempotent_tools": ["search"],
                "retry_unsafe": True,
            },
            "local": {
                "command": "uvx",
                "args": ["some-server"],
                "idempotent_tools": ["put"],
            },
        }
    )

    assert settings.idempotent_tools_by_server == {
        "remote": {"search"},
        "local": {"put"},
    }
    assert settings.retry_unsafe_servers == {"remote"}
    # SSEMCPServer uses extra="forbid": loading succeeded, so the keys were
    # stripped from the transport config.
    assert settings.mcp_servers["remote"].url == "http://localhost:11403/sse"


def test_documented_default_matches_the_code() -> None:
    assert DEFAULT_MCP_TOOL_RETRY_COUNT == 1
    readme = (ROOT / "README.md").read_text()
    assert "| `MCP_BRIDGE_TOOL_RETRY_COUNT` | `1` |" in readme
