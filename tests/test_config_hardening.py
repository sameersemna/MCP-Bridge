import asyncio
import json
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
from mcp_bridge.mcp_clients.SseClient import SseClient
from mcp_bridge.mcp_clients.StdioClient import StdioClient
from mcp_bridge.health.manager import manager
from mcp_bridge.openai_clients import chatCompletion as chat_completion_module
from mcp_bridge.openai_clients import utils as openai_utils
from mcp_bridge.openai_clients.streamChatCompletion import merge_streaming_tool_calls
from mcp_bridge.telemetry import setup_tracing
from mcp_bridge.tool_mappers.mcp2openaiConverters import mcp2openai


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


def test_settings_cached_flag_is_captured_and_stripped() -> None:
    """The `cached` flag must be captured into `cached_mcp_servers` AND removed
    from the server config, so `extra="forbid"` transport models (e.g. SSE/HTTP)
    accept the server entry. Regression test for the config-load crash."""
    settings = Settings(
        mcp_servers={
            "google-search": {
                "type": "sse",
                "url": "http://localhost:11403/sse",
                "cached": True,
            },
            "fetch": {
                "command": "uvx",
                "args": ["mcp-server-fetch"],
                "cached": True,
            },
            "memory": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-memory"],
            },
        }
    )

    # The cached servers are captured.
    assert settings.cached_mcp_servers == {"google-search", "fetch"}
    # The non-cached server is not.
    assert "memory" not in settings.cached_mcp_servers
    # The SSE server (extra="forbid") loaded successfully, meaning `cached` was
    # stripped from its config.
    assert settings.mcp_servers["google-search"].url == "http://localhost:11403/sse"


def test_transport_selection_uses_sse_client_for_sse_style_urls() -> None:
    server_config = SSEMCPServer(
        type="http",
        url="http://localhost:11403/sse",
        auth={"type": "none"},
    )

    client_class = MCPClientManager._get_client_class(server_config)

    assert client_class is SseClient


def test_http_transport_supports_jsonrpc_post_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload
            self.headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

        async def aread(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        @property
        def text(self) -> str:
            return json.dumps(self._payload)

    class FakeStream:
        def __init__(self, response: FakeResponse) -> None:
            self._response = response

        async def __aenter__(self) -> FakeResponse:
            return self._response

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, object]] = []

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method: str, url: str, headers: dict[str, str] | None = None, json: object | None = None):
            self.calls.append((url, method, json))
            return FakeStream(FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "demo", "version": "1.0"}}}))

    import mcp_bridge.mcp_clients.SseClient as sse_module

    monkeypatch.setattr(sse_module.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())

    session = sse_module.HttpMcpSession(url="https://mcp.grep.app")

    response = asyncio.run(session.initialize())

    assert response.protocolVersion == "2024-11-05"


def test_http_transport_sends_initialized_notification_with_empty_params(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload
            self.headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

        async def aread(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    class FakeStream:
        def __init__(self, response: FakeResponse) -> None:
            self._response = response

        async def __aenter__(self) -> FakeResponse:
            return self._response

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method: str, url: str, headers: dict[str, str] | None = None, json: object | None = None):
            self.calls.append({"method": method, "url": url, "payload": json})
            return FakeStream(FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "demo", "version": "1.0"}}}))

    import mcp_bridge.mcp_clients.SseClient as sse_module

    client = FakeClient()
    monkeypatch.setattr(sse_module.httpx, "AsyncClient", lambda *args, **kwargs: client)

    session = sse_module.HttpMcpSession(url="https://mcp.grep.app")
    asyncio.run(session.initialize())

    notification_payload = next(call["payload"] for call in client.calls if call["payload"].get("method") == "notifications/initialized")
    assert notification_payload.get("method") == "notifications/initialized"
    assert notification_payload.get("params") == {}


def test_http_transport_treats_empty_notification_response_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyResponse:
        status_code = 202
        headers = {"content-length": "0"}

        def raise_for_status(self) -> None:
            return None

        async def aread(self) -> bytes:
            return b""

        async def aiter_lines(self):
            if False:
                yield ""
            return

        async def __aenter__(self) -> "EmptyResponse":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method: str, url: str, headers: dict[str, str] | None = None, json: object | None = None):
            self.calls.append({"method": method, "url": url, "payload": json})
            return EmptyResponse()

    import mcp_bridge.mcp_clients.SseClient as sse_module

    client = FakeClient()
    monkeypatch.setattr(sse_module.httpx, "AsyncClient", lambda *args, **kwargs: client)

    session = sse_module.HttpMcpSession(url="https://mcp.grep.app")
    response = asyncio.run(session._post_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))

    assert response == {}


def test_http_transport_normalizes_null_params_to_empty_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

        async def aread(self) -> bytes:
            return b'{"jsonrpc":"2.0","id":1,"result":{}}'

    class FakeStream:
        def __init__(self, response: FakeResponse) -> None:
            self._response = response

        async def __aenter__(self) -> FakeResponse:
            return self._response

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method: str, url: str, headers: dict[str, str] | None = None, json: object | None = None):
            self.calls.append({"method": method, "url": url, "payload": json})
            return FakeStream(FakeResponse())

    import mcp_bridge.mcp_clients.SseClient as sse_module

    client = FakeClient()
    monkeypatch.setattr(sse_module.httpx, "AsyncClient", lambda *args, **kwargs: client)

    session = sse_module.HttpMcpSession(url="https://mcp.grep.app")
    response = asyncio.run(session._send_request("tools/list", None, result_type=SimpleNamespace(model_validate=lambda value: value)))

    assert response == {}
    assert client.calls[0]["payload"]["params"] == {}


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


def test_chat_completion_add_tools_initializes_client_manager_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="searchGitHub",
                        description="Search GitHub",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    class FakeClient:
        def __init__(self) -> None:
            self.name = "demo"
            self.config = None
            self.session = FakeSession()

        async def _wait_for_session(self, *args: object, **kwargs: object) -> None:
            return None

    async def fake_initialize() -> None:
        openai_utils.ClientManager.clients = {"demo": FakeClient()}

    monkeypatch.setattr(openai_utils.ClientManager, "initialize", fake_initialize)
    monkeypatch.setattr(openai_utils.ClientManager, "get_clients", lambda: list(openai_utils.ClientManager.clients.items()))

    request = SimpleNamespace(
        messages=[SimpleNamespace(role="user", content="Find a GitHub example")],
        tools=None,
    )

    result = asyncio.run(openai_utils.chat_completion_add_tools(request))

    assert len(result.tools) == 1
    tool = result.tools[0]
    assert getattr(getattr(tool, "function", None), "name", None) == "searchGitHub"


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


def test_get_client_from_tool_does_not_wait_for_unready_clients() -> None:
    class UnreadyClient:
        def __init__(self):
            self.name = "slow"
            self.session = None

        async def _wait_for_session(self, timeout: int | None = None, http_error: bool = True):
            await asyncio.sleep(0.2)
            raise TimeoutError("not ready")

    class FastClient:
        def __init__(self):
            self.name = "fast"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="alpha")])

    manager = MCPClientManager()
    manager.clients = {"slow": UnreadyClient(), "fast": FastClient()}

    start = time.perf_counter()
    result = asyncio.run(asyncio.wait_for(manager.get_client_from_tool("alpha", timeout=0.2), timeout=0.3))
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


def test_get_client_from_tool_matches_normalized_tool_names() -> None:
    class NormalizedToolClient:
        def __init__(self):
            self.name = "normalized"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="google_search")])

    manager = MCPClientManager()
    manager.clients = {"normalized": NormalizedToolClient()}

    result = asyncio.run(asyncio.wait_for(manager.get_client_from_tool("google-search", timeout=0.2), timeout=0.3))

    assert result is not None
    assert getattr(result, "name", None) == "normalized"


def test_resolve_tool_returns_client_and_actual_tool_name() -> None:
    class NormalizedToolClient:
        def __init__(self):
            self.name = "normalized"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="google_search")])

    manager = MCPClientManager()
    manager.clients = {"normalized": NormalizedToolClient()}

    resolved = asyncio.run(asyncio.wait_for(manager.resolve_tool("google-search", timeout=0.2), timeout=0.3))

    assert resolved is not None
    client, actual_name = resolved
    assert getattr(client, "name", None) == "normalized"
    assert actual_name == "google_search"


def test_resolve_tool_returns_none_when_no_match() -> None:
    class NormalizedToolClient:
        def __init__(self):
            self.name = "normalized"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="google_search")])

    manager = MCPClientManager()
    manager.clients = {"normalized": NormalizedToolClient()}

    resolved = asyncio.run(asyncio.wait_for(manager.resolve_tool("nonexistent-tool", timeout=0.2), timeout=0.3))

    assert resolved is None


def test_resolve_tool_falls_back_to_single_tool_server_name() -> None:
    # Calling a *server* name that exposes exactly one tool should dispatch to
    # that tool directly (the common "called the server" case).
    class SingleToolClient:
        def __init__(self):
            self.name = "memory"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="search_nodes")])

    manager = MCPClientManager()
    manager.clients = {"memory": SingleToolClient()}

    resolved = asyncio.run(asyncio.wait_for(manager.resolve_tool("memory", timeout=0.2), timeout=0.3))

    assert resolved is not None
    client, actual_name = resolved
    assert getattr(client, "name", None) == "memory"
    assert actual_name == "search_nodes"


def test_resolve_tool_does_not_fallback_when_server_has_multiple_tools() -> None:
    # A server name with multiple tools cannot be disambiguated reliably, so
    # resolution should return None and let the corrective error path handle it.
    class MultiToolClient:
        def __init__(self):
            self.name = "memory"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="create_entities"),
                    SimpleNamespace(name="search_nodes"),
                ]
            )

    manager = MCPClientManager()
    manager.clients = {"memory": MultiToolClient()}

    resolved = asyncio.run(asyncio.wait_for(manager.resolve_tool("memory", timeout=0.2), timeout=0.3))

    assert resolved is None


def test_suggest_tools_returns_close_matches() -> None:
    class Client:
        def __init__(self):
            self.name = "memory"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="create_entities"),
                    SimpleNamespace(name="search_nodes"),
                    SimpleNamespace(name="read_graph"),
                ]
            )

    manager = MCPClientManager()
    manager.clients = {"memory": Client()}

    suggestions = asyncio.run(asyncio.wait_for(manager.suggest_tools("search", timeout=0.2), timeout=0.3))

    assert "search_nodes" in suggestions


def test_suggest_tools_prioritizes_server_name_tools() -> None:
    # Calling a *server* name should suggest that server's tools, not unrelated
    # fuzzy matches from other servers (e.g. Redis commands for 'memory').
    class MemoryClient:
        def __init__(self):
            self.name = "memory"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="create_entities"),
                    SimpleNamespace(name="search_nodes"),
                    SimpleNamespace(name="read_graph"),
                ]
            )

    class RedisClient:
        def __init__(self):
            self.name = "redis"
            self.session = SimpleNamespace(list_tools=self.list_tools)

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="smembers"),
                    SimpleNamespace(name="zrem"),
                    SimpleNamespace(name="srem"),
                ]
            )

    manager = MCPClientManager()
    manager.clients = {"memory": MemoryClient(), "redis": RedisClient()}

    suggestions = asyncio.run(asyncio.wait_for(manager.suggest_tools("memory", timeout=0.2), timeout=0.3))

    assert suggestions == ["create_entities", "search_nodes", "read_graph"]


def test_call_tool_returns_error_result_when_no_client_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_client_from_tool(*args, **kwargs):
        return None

    monkeypatch.setattr(openai_utils.ClientManager, "get_client_from_tool", fake_get_client_from_tool)

    result = asyncio.run(openai_utils.call_tool("missing-tool", "{}"))

    assert result is not None
    assert result.isError is True
    assert "No MCP client" in result.content[0].text


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


def test_tool_result_cache_serves_near_duplicate_queries() -> None:
    cache = openai_utils.ToolResultCache()
    result = {"isError": False, "content": [{"type": "text", "text": "results"}]}

    cache.put("google_search", '"حجر الأساس" "بدعة" "فتوى" "الشيخ" "مدرسة" "مسجد" "سلفي" "تحريم" "وثني" "مشركين" "أصل" "تاريخ" "طقوس" "تقليد"', result)

    # A near-duplicate (one keyword appended) should hit the cache.
    near = '"حجر الأساس" "بدعة" "فتوى" "الشيخ" "مدرسة" "مسجد" "سلفي" "تحريم" "وثني" "مشركين" "أصل" "تاريخ" "طقوس" "تقليد" "الكفار"'
    assert cache.get("google_search", near) is result

    # A genuinely different query should miss.
    assert cache.get("google_search", "Sang e Buniyaad foundation stone ceremony") is None


def test_tool_result_cache_does_not_cache_errors() -> None:
    cache = openai_utils.ToolResultCache()
    error_result = {"isError": True, "content": [{"type": "text", "text": "failed"}]}

    cache.put("google_search", "some query", error_result)

    assert cache.get("google_search", "some query") is None
    assert len(cache) == 0


def test_call_tools_serves_near_duplicate_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_call_tool(name: str, payload: str, timeout: float | None = None):
        calls.append(payload)
        return {"isError": False, "content": [{"type": "text", "text": "web result"}]}

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)

    cache = openai_utils.ToolResultCache()
    q1 = '"حجر الأساس" "بدعة" "فتوى" "الشيخ" "مدرسة" "مسجد" "سلفي" "تحريم" "وثني" "مشركين" "أصل" "تاريخ" "طقوس" "تقليد"'
    q2 = q1 + ' "الكفار"'

    # First call hits the web and is cached.
    r1 = asyncio.run(openai_utils.call_tools([("google_search", json.dumps({"query": q1}))], result_cache=cache))
    # Second near-duplicate call is served from cache (no web call).
    r2 = asyncio.run(openai_utils.call_tools([("google_search", json.dumps({"query": q2}))], result_cache=cache))

    assert len(calls) == 1
    assert r1[0]["content"][0]["text"] == "web result"
    assert r2[0]["content"][0]["text"] == "web result"


def test_persistent_tool_cache_roundtrips_across_instances(tmp_path) -> None:
    cache_dir = str(tmp_path / "tool_cache")
    result = {"isError": False, "content": [{"type": "text", "text": "persisted result"}]}

    # First instance writes.
    cache1 = openai_utils.PersistentToolCache(cache_dir=cache_dir, ttl_seconds=3600)
    cache1.put("google_search", "some query", result)

    # A brand-new instance (simulating a new request / restart) reads it back.
    cache2 = openai_utils.PersistentToolCache(cache_dir=cache_dir, ttl_seconds=3600)
    hit = cache2.get("google_search", "some query")

    assert hit is not None
    assert hit.content[0].text == "persisted result"


def test_persistent_tool_cache_respects_ttl(tmp_path) -> None:
    cache_dir = str(tmp_path / "tool_cache")
    result = {"isError": False, "content": [{"type": "text", "text": "stale"}]}

    cache = openai_utils.PersistentToolCache(cache_dir=cache_dir, ttl_seconds=0)
    cache.put("google_search", "some query", result)

    # TTL of 0 means the entry is immediately expired.
    assert cache.get("google_search", "some query") is None


def test_persistent_tool_cache_does_not_cache_errors(tmp_path) -> None:
    cache_dir = str(tmp_path / "tool_cache")
    cache = openai_utils.PersistentToolCache(cache_dir=cache_dir, ttl_seconds=3600)
    cache.put("google_search", "some query", {"isError": True, "content": [{"type": "text", "text": "failed"}]})

    assert cache.get("google_search", "some query") is None


def test_call_tools_serves_exact_match_from_persistent_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: list[str] = []

    async def fake_call_tool(name: str, payload: str, timeout: float | None = None):
        calls.append(payload)
        return {"isError": False, "content": [{"type": "text", "text": "web result"}]}

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)

    cache_dir = str(tmp_path / "tool_cache")
    persistent = openai_utils.PersistentToolCache(cache_dir=cache_dir, ttl_seconds=3600)
    query = "Hajr al-Asas foundation stone ceremony"

    # First call hits the web and persists.
    asyncio.run(openai_utils.call_tools([("google_search", json.dumps({"query": query}))], persistent_cache=persistent))
    # Second call (same exact query) is served from disk.
    asyncio.run(openai_utils.call_tools([("google_search", json.dumps({"query": query}))], persistent_cache=persistent))

    assert len(calls) == 1


def test_call_tools_cache_gated_by_cache_enabled_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-server opt-in caching: a tool whose server is NOT opted in must not
    be served from (or written to) the cache, even for an exact-match query."""
    calls: list[str] = []

    async def fake_call_tool(name: str, payload: str, timeout: float | None = None):
        calls.append(payload)
        return {"isError": False, "content": [{"type": "text", "text": "web result"}]}

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)

    cache = openai_utils.ToolResultCache()
    query = "Hajr al-Asas foundation stone ceremony"

    # `cache_enabled` returns False for this tool (its server is not opted in),
    # so the result must NOT be cached and every call must hit the web.
    def cache_enabled(name: str) -> bool:
        return False

    asyncio.run(openai_utils.call_tools(
        [("google_search", json.dumps({"query": query}))],
        result_cache=cache,
        cache_enabled=cache_enabled,
    ))
    asyncio.run(openai_utils.call_tools(
        [("google_search", json.dumps({"query": query}))],
        result_cache=cache,
        cache_enabled=cache_enabled,
    ))

    # Both calls hit the web because caching is disabled for this tool.
    assert len(calls) == 2
    assert len(cache) == 0


def test_call_tools_cache_enabled_when_predicate_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `cache_enabled` returns True, an exact-match query is served from
    the in-memory cache on the second call."""
    calls: list[str] = []

    async def fake_call_tool(name: str, payload: str, timeout: float | None = None):
        calls.append(payload)
        return {"isError": False, "content": [{"type": "text", "text": "web result"}]}

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)

    cache = openai_utils.ToolResultCache()
    query = "Hajr al-Asas foundation stone ceremony"

    def cache_enabled(name: str) -> bool:
        return True

    asyncio.run(openai_utils.call_tools(
        [("google_search", json.dumps({"query": query}))],
        result_cache=cache,
        cache_enabled=cache_enabled,
    ))
    asyncio.run(openai_utils.call_tools(
        [("google_search", json.dumps({"query": query}))],
        result_cache=cache,
        cache_enabled=cache_enabled,
    ))

    # Second call is served from cache (only one web call).
    assert len(calls) == 1
    assert len(cache) == 1


def test_chat_completion_add_tools_does_not_wait_for_every_unavailable_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnavailableSession:
        name = "unavailable"
        session = None

        async def _wait_for_session(self, timeout: int | None = None, http_error: bool = True):
            await asyncio.sleep(timeout if timeout is not None else 0.05)
            raise TimeoutError("not ready")

    monkeypatch.setattr(openai_utils, "DEFAULT_MCP_SESSION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(openai_utils.ClientManager, "get_clients", lambda: [("slow", UnavailableSession()), ("fast", UnavailableSession())])

    start = time.perf_counter()
    request = SimpleNamespace(tools=[])
    result = asyncio.run(openai_utils.chat_completion_add_tools(request))
    elapsed = time.perf_counter() - start

    assert result.tools == []
    assert elapsed < 0.08


def test_mcp2openai_preserves_tool_name_for_search_github() -> None:
    tool = SimpleNamespace(name="searchGitHub", description="Search GitHub", inputSchema={"type": "object"})

    converted = mcp2openai(tool)

    assert converted.function.name == "searchGitHub"
    assert "GitHub repositories" in converted.function.description


def test_maybe_add_tool_selection_instructions_injects_system_hint_for_github_search() -> None:
    request = SimpleNamespace(
        tools=[SimpleNamespace(name="searchGitHub", description="Search GitHub", inputSchema={"type": "object"})],
        messages=[SimpleNamespace(role="user", content="Find a React useEffect cleanup example")],
    )

    updated_request = openai_utils.maybe_add_tool_selection_instructions(request)

    assert updated_request.messages[0].role == "system"
    assert "searchGitHub" in updated_request.messages[0].content
    assert updated_request.messages[1].role == "user"


def test_get_tool_timeout_uses_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_BRIDGE_TOOL_TIMEOUT_SECONDS", "45")

    assert chat_completion_module.get_tool_timeout_seconds() == 45


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
