"""HTTP contract regressions (P0-10).

404 instead of 500 for unknown servers, no shared mutable body default,
dispatch by canonical tool name, an OpenAI-style 501 for streaming completions,
and an upstream status that is not swallowed by /v1/models.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

import mcp_bridge.mcp_server.server as aggregated_server
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.mcpManagement import server as management_server
from mcp_bridge.mcpManagement import tools as management_tools
from mcp_bridge.mcpManagement.tools import call_tool as call_tool_endpoint

ROOT = Path(__file__).resolve().parents[1]


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def call_tool(self, name: str, arguments: object) -> CallToolResult:
        self.calls.append((name, arguments))
        return CallToolResult(
            content=[TextContent(type="text", text="ok")], isError=False
        )


def _management_app() -> FastAPI:
    app = FastAPI()
    app.include_router(management_server.router, prefix="/mcp")
    app.include_router(management_tools.router, prefix="/mcp")
    return app


def _run_in_clean_interpreter(script: str) -> None:
    """Run a snippet in a fresh interpreter.

    Other test modules replace `sys.modules["lmos_openai_types"]` with a stub
    and never restore it, so importing `mcp_bridge.endpoints` during collection
    fails depending on file order. A subprocess isolates these checks from that
    global state.
    """
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"subprocess failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def test_get_client_returns_none_for_an_unknown_server(monkeypatch) -> None:
    monkeypatch.setattr(ClientManager, "clients", {}, raising=False)
    assert ClientManager.get_client("missing") is None


def test_unknown_server_returns_404_not_500(monkeypatch) -> None:
    monkeypatch.setattr(ClientManager, "clients", {}, raising=False)

    response = TestClient(_management_app(), raise_server_exceptions=False).get(
        "/mcp/servers/missing/tools"
    )

    assert response.status_code == 404


def test_tool_call_arguments_default_is_not_a_shared_mutable_dict() -> None:
    default = inspect.signature(call_tool_endpoint).parameters["arguments"].default
    assert not isinstance(default, dict)


def test_tool_call_dispatches_the_resolved_name(monkeypatch) -> None:
    fake = _FakeClient()

    async def resolve(name: str, timeout: float | None = None):
        return (fake, "canonical_name")

    monkeypatch.setattr(ClientManager, "resolve_tool", resolve, raising=False)

    response = TestClient(_management_app()).post(
        "/mcp/tools/alias/call", json={"query": "x"}
    )

    assert response.status_code == 200
    assert fake.calls == [("canonical_name", {"query": "x"})]


def test_tool_call_without_a_body_passes_empty_arguments(monkeypatch) -> None:
    fake = _FakeClient()

    async def resolve(name: str, timeout: float | None = None):
        return (fake, "canonical_name")

    monkeypatch.setattr(ClientManager, "resolve_tool", resolve, raising=False)

    response = TestClient(_management_app()).post("/mcp/tools/alias/call")

    assert response.status_code == 200
    assert fake.calls == [("canonical_name", {})]


def test_aggregated_server_dispatches_the_resolved_name(monkeypatch) -> None:
    fake = _FakeClient()

    async def resolve(name: str, timeout: float | None = None):
        return (fake, "canonical_name")

    monkeypatch.setattr(ClientManager, "resolve_tool", resolve, raising=False)

    asyncio.run(aggregated_server.handle_call_tool("alias", {"query": "x"}))

    assert fake.calls == [("canonical_name", {"query": "x"})]


def test_streaming_completions_returns_an_openai_style_501() -> None:
    _run_in_clean_interpreter(
        """
from fastapi import FastAPI
from fastapi.testclient import TestClient
import mcp_bridge.endpoints as endpoints

app = FastAPI()
app.include_router(endpoints.router)
response = TestClient(app, raise_server_exceptions=False).post(
    "/v1/completions",
    json={"model": "some-model", "prompt": "hello", "stream": True},
)
assert response.status_code == 501, response.status_code
error = response.json()["error"]
assert error["code"] == "unsupported", error
assert "chat/completions" in error["message"], error
print("ok")
"""
    )


def test_models_propagates_the_upstream_status() -> None:
    _run_in_clean_interpreter(
        """
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import mcp_bridge.endpoints as endpoints

upstream = httpx.Response(
    429,
    json={"error": {"message": "rate limited"}},
    request=httpx.Request("GET", "http://upstream/v1/models"),
)

class Upstream:
    async def get(self, path):
        return upstream

@asynccontextmanager
async def fake_get_client(request=None):
    yield Upstream()

endpoints.get_client = fake_get_client
app = FastAPI()
app.include_router(endpoints.router)
response = TestClient(app).get("/v1/models")
assert response.status_code == 429, response.status_code
assert response.json()["error"]["message"] == "rate limited"
print("ok")
"""
    )
