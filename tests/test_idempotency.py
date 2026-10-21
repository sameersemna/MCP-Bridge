"""Idempotency-Key semantics (P1-03)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

from mcp_bridge.idempotency import InMemoryIdempotencyStore
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.mcpManagement import tools as management_tools

ROOT = Path(__file__).resolve().parents[1]


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name: str, arguments: object) -> CallToolResult:
        self.calls += 1
        return CallToolResult(
            content=[TextContent(type="text", text="ok")], isError=False
        )


def _tool_app() -> FastAPI:
    app = FastAPI()
    app.include_router(management_tools.router, prefix="/mcp")
    return app


def _use_memory_store(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import mcp_bridge.idempotency as idempotency_module

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(idempotency_module, "_store", None, raising=False)


def test_store_new_in_progress_replay_and_mismatch() -> None:
    async def run() -> None:
        store = InMemoryIdempotencyStore(ttl_seconds=60)
        state, entry = await store.begin("key", "fingerprint")
        assert state == "new" and entry is None

        state, _ = await store.begin("key", "fingerprint")
        assert state == "in_progress"

        await store.complete("key", status=200, body={"ok": True})
        state, entry = await store.begin("key", "fingerprint")
        assert state == "replay"
        assert entry is not None and entry.body == {"ok": True}

        state, _ = await store.begin("key", "different")
        assert state == "mismatch"

    asyncio.run(run())


def test_store_release_allows_retry() -> None:
    async def run() -> None:
        store = InMemoryIdempotencyStore(ttl_seconds=60)
        await store.begin("key", "fingerprint")
        await store.release("key")
        state, _ = await store.begin("key", "fingerprint")
        assert state == "new"

    asyncio.run(run())


def test_store_expires_entries() -> None:
    async def run() -> None:
        store = InMemoryIdempotencyStore(ttl_seconds=0)
        await store.begin("key", "fingerprint")
        state, _ = await store.begin("key", "fingerprint")
        assert state == "new"

    asyncio.run(run())


def test_tool_call_replays_without_invoking_the_tool_twice(monkeypatch) -> None:
    _use_memory_store(monkeypatch)
    fake = _FakeClient()

    async def resolve(name: str, timeout: float | None = None):
        return (fake, "canonical")

    monkeypatch.setattr(ClientManager, "resolve_tool", resolve, raising=False)

    client = TestClient(_tool_app())
    body = {"query": "x"}
    key = "test-tool-replay-key"

    first = client.post(
        "/mcp/tools/alias/call", json=body, headers={"Idempotency-Key": key}
    )
    second = client.post(
        "/mcp/tools/alias/call", json=body, headers={"Idempotency-Key": key}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert fake.calls == 1
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert second.json()["isError"] is False


def test_tool_call_rejects_a_reused_key_with_a_different_body(monkeypatch) -> None:
    _use_memory_store(monkeypatch)
    fake = _FakeClient()

    async def resolve(name: str, timeout: float | None = None):
        return (fake, "canonical")

    monkeypatch.setattr(ClientManager, "resolve_tool", resolve, raising=False)

    client = TestClient(_tool_app())
    key = "test-tool-mismatch-key"

    first = client.post(
        "/mcp/tools/alias/call", json={"query": "a"}, headers={"Idempotency-Key": key}
    )
    second = client.post(
        "/mcp/tools/alias/call", json={"query": "b"}, headers={"Idempotency-Key": key}
    )

    assert first.status_code == 200
    assert second.status_code == 422
    assert fake.calls == 1


def _run_in_clean_interpreter(script: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True
    )
    assert completed.returncode == 0, (
        f"subprocess failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def test_chat_completions_replay_mismatch_and_streaming_rejection() -> None:
    _run_in_clean_interpreter(
        """
import os
os.environ.pop("REDIS_URL", None)

from fastapi import FastAPI
from fastapi.testclient import TestClient
import mcp_bridge.endpoints as endpoints

calls = []

class FakeResponse:
    choices = []
    def model_dump(self, **kwargs):
        return {"id": "chatcmpl-test", "choices": []}

async def fake_chat_completions(request, http_request, trace_logger):
    calls.append(1)
    return FakeResponse()

endpoints.chat_completions = fake_chat_completions
app = FastAPI()
app.include_router(endpoints.router)
client = TestClient(app)

body = {"model": "some-model", "messages": [{"role": "user", "content": "hi"}]}
key = "test-chat-replay-key"

first = client.post("/v1/chat/completions", json=body, headers={"Idempotency-Key": key})
second = client.post("/v1/chat/completions", json=body, headers={"Idempotency-Key": key})
assert first.status_code == 200, first.text
assert second.status_code == 200, second.text
assert len(calls) == 1, len(calls)
assert second.headers.get("Idempotent-Replayed") == "true", second.headers

mismatch = client.post(
    "/v1/chat/completions",
    json={**body, "temperature": 0.5},
    headers={"Idempotency-Key": key},
)
assert mismatch.status_code == 422, mismatch.text

streaming = client.post(
    "/v1/chat/completions",
    json={**body, "stream": True},
    headers={"Idempotency-Key": "test-chat-stream-key"},
)
assert streaming.status_code == 400, streaming.text
assert streaming.json()["error"]["code"] == "idempotency_key"

print("ok")
"""
    )
