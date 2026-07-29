import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from mcp_bridge.config.file import load_config
from mcp_bridge.config.final import Settings, SSEMCPServer
from mcp_bridge.logging import redact_sensitive_data
from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient
from mcp_bridge.mcp_clients.McpClientManager import MCPClientManager
from mcp_bridge.mcp_clients.StdioClient import StdioClient
from mcp_bridge.health.manager import manager
from mcp_bridge.openai_clients import utils as openai_utils
from mcp_bridge.openai_clients.streamChatCompletion import merge_streaming_tool_calls
from mcp_bridge.telemetry import setup_tracing


def test_load_config_rejects_path_traversal(tmp_path: Path) -> None:
    secret_config = tmp_path / "secret.json"
    secret_config.write_text('{"inference_server": {"base_url": "http://example.com/v1"}}', encoding="utf-8")

    with pytest.raises(ValueError, match="outside"):
        load_config(str(secret_config))


def test_redact_sensitive_data_masks_secrets() -> None:
    payload = {"api_key": "secret", "nested": {"token": "abc123"}, "message": "ok"}

    redacted = redact_sensitive_data(payload)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["message"] == "ok"


def test_settings_reject_invalid_ports() -> None:
    with pytest.raises(ValidationError):
        Settings(network={"port": 70000})


def test_settings_reject_invalid_inference_base_url() -> None:
    with pytest.raises(ValidationError, match="base_url"):
        Settings(inference_server={"base_url": "not-a-url"})


def test_settings_reject_invalid_mcp_server_config() -> None:
    with pytest.raises(ValidationError, match="command|url|image"):
        Settings(mcp_servers={"bad": {"foo": "bar"}})


def test_settings_accepts_http_style_mcp_server_config() -> None:
    settings = Settings(
        mcp_servers={
            "google-search": {
                "type": "http",
                "url": "http://localhost:11403/mcp",
                "auth": {"type": "none"},
                "requestTimeout": 10000,
            }
        }
    )

    server = settings.mcp_servers["google-search"]

    assert server.type == "http"
    assert server.url == "http://localhost:11403/mcp"
    assert server.auth == {"type": "none"}
    assert server.requestTimeout == 10000


def test_settings_preserves_disabled_flag_for_stdio_servers() -> None:
    settings = Settings(
        mcp_servers={
            "fetch": {
                "command": "uvx",
                "args": ["mcp-server-fetch"],
                "disabled": True,
            }
        }
    )

    server = settings.mcp_servers["fetch"]

    assert "fetch" in settings.disabled_mcp_servers
    assert getattr(server, "disabled", None) is None


def test_setup_tracing_is_idempotent() -> None:
    app = FastAPI()

    setup_tracing(app)
    setup_tracing(app)

    assert getattr(app.state, "_tracing_initialized", False) is True


def test_get_client_from_tool_returns_none_when_discovery_times_out() -> None:
    class SlowSession:
        async def list_tools(self):
            await asyncio.sleep(0.05)
            return SimpleNamespace(tools=[])

    class StubClient:
        def __init__(self, session):
            self.session = session

    manager = MCPClientManager()
    manager.clients = {"slow": StubClient(SlowSession())}

    result = asyncio.run(manager.get_client_from_tool("missing-tool", timeout=0.01))

    assert result is None


def test_get_client_from_tool_uses_default_timeout_when_lookup_hangs() -> None:
    class HangingSession:
        async def list_tools(self):
            await asyncio.sleep(1)
            return SimpleNamespace(tools=[])

    class StubClient:
        def __init__(self, session):
            self.session = session

    manager = MCPClientManager()
    manager.clients = {"slow": StubClient(HangingSession())}

    result = asyncio.run(asyncio.wait_for(manager.get_client_from_tool("missing-tool"), timeout=3.0))

    assert result is None


def test_get_client_from_tool_returns_fast_when_a_slow_client_is_present() -> None:
    class SlowClient:
        def __init__(self):
            self.session = None

        async def list_tools(self):
            await asyncio.sleep(0.2)
            return SimpleNamespace(tools=[])

    class FastClient:
        def __init__(self):
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="alpha")])

    manager = MCPClientManager()
    manager.clients = {"slow": SlowClient(), "fast": FastClient()}

    start = time.perf_counter()
    result = asyncio.run(manager.get_client_from_tool("alpha", timeout=0.1))
    elapsed = time.perf_counter() - start

    assert result is not None
    assert elapsed < 0.15


def test_get_client_from_tool_waits_for_session_to_become_ready() -> None:
    class DelayedSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="alpha")])

    class StubClient:
        def __init__(self):
            self.session = None

        async def list_tools(self):
            if self.session is None:
                await asyncio.sleep(0.05)
                self.session = DelayedSession()
            return await self.session.list_tools()

    manager = MCPClientManager()
    manager.clients = {"ready-later": StubClient()}

    result = asyncio.run(asyncio.wait_for(manager.get_client_from_tool("alpha"), timeout=0.2))

    assert result is not None


def test_get_client_from_tool_does_not_stop_after_first_no_match() -> None:
    class NoMatchClient:
        def __init__(self):
            self.name = "no-match"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    class DelayedMatchClient:
        def __init__(self):
            self.name = "match-later"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            await asyncio.sleep(0.05)
            return SimpleNamespace(tools=[SimpleNamespace(name="alpha")])

    manager = MCPClientManager()
    manager.clients = {"no-match": NoMatchClient(), "match-later": DelayedMatchClient()}

    result = asyncio.run(asyncio.wait_for(manager.get_client_from_tool("alpha", timeout=0.2), timeout=0.3))

    assert result is not None
    assert getattr(result, "name", None) == "match-later"


def test_stdio_client_adds_compatibility_path_to_subprocess_environment() -> None:
    config = SimpleNamespace(
        command=sys.executable,
        args=[],
        env={},
        model_copy=lambda deep=True: SimpleNamespace(command=sys.executable, args=[], env={}, model_fields_set=set()),
        model_fields_set=set(),
    )

    client = StdioClient("demo", config)

    pythonpath = client.config.env.get("PYTHONPATH", "")
    compat_dir = str(Path(__file__).resolve().parent.parent / "mcp_bridge" / "compat")

    assert compat_dir in pythonpath.split(os.pathsep)


def test_session_maintainer_stops_after_initial_startup_failure() -> None:
    class BrokenClient(GenericMcpClient):
        def __init__(self) -> None:
            super().__init__("broken")

        async def _maintain_session(self) -> None:
            raise RuntimeError("startup failed")

    client = BrokenClient()

    asyncio.run(asyncio.wait_for(client._session_maintainer(), timeout=0.2))


def test_call_tool_uses_a_longer_default_timeout() -> None:
    class SlowSession:
        async def call_tool(self, name: str, arguments: dict | None):
            await asyncio.sleep(2.2)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], isError=False)

    class StubClient(GenericMcpClient):
        def __init__(self) -> None:
            super().__init__("slow")
            self.session = SimpleNamespace(call_tool=SlowSession().call_tool)

        async def _maintain_session(self) -> None:
            return None

    client = StubClient()
    result = asyncio.run(client.call_tool("fetch", {"url": "https://example.com"}))

    assert result is not None
    assert result.isError is False
    assert result.content[0].text == "ok"


def test_call_tools_runs_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_call_tool(name: str, payload: str, timeout: float | None = None):
        await asyncio.sleep(0.05)
        return {"name": name, "payload": payload}

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)

    start = time.perf_counter()
    results = asyncio.run(openai_utils.call_tools([("alpha", "{}"), ("beta", "{}")]))
    elapsed = time.perf_counter() - start

    assert [result["name"] for result in results] == ["alpha", "beta"]
    assert elapsed < 0.09


def test_merge_streaming_tool_calls_accumulates_multiple_calls() -> None:
    deltas = [
        SimpleNamespace(index=0, id="call-1", function=SimpleNamespace(name="alpha", arguments='{"a":')),
        SimpleNamespace(index=0, function=SimpleNamespace(name=None, arguments='1}')),
        SimpleNamespace(index=1, id="call-2", function=SimpleNamespace(name="beta", arguments='{"b":2}')),
    ]

    merged = merge_streaming_tool_calls([], deltas)

    assert len(merged) == 2
    assert merged[0]["name"] == "alpha"
    assert merged[0]["arguments"] == '{"a":1}'
    assert merged[0]["id"] == "call-1"
    assert merged[1]["name"] == "beta"
    assert merged[1]["arguments"] == '{"b":2}'
    assert merged[1]["id"] == "call-2"


def test_manager_reports_mcp_server_health() -> None:
    class StubClient:
        def __init__(self, session):
            self.session = session

    class StubRegistry:
        def get_clients(self):
            return [("offline", StubClient(None))]

    health = manager.get_mcp_server_health(StubRegistry())

    assert any(item.name == "offline" and item.status == "offline" for item in health)


def test_manager_exposes_latest_mcp_inventory_summary() -> None:
    manager.last_inventory = {
        "enabled": ["google-search"],
        "disabled": ["fetch-old"],
        "failed": [],
        "active": ["google-search"],
    }

    inventory = manager.get_mcp_inventory()

    assert inventory == {
        "enabled": ["google-search"],
        "disabled": ["fetch-old"],
        "failed": [],
        "active": ["google-search"],
    }
