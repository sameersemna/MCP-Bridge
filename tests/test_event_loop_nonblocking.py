"""Hot-path I/O must not block the event loop (P1-04)."""

from __future__ import annotations

import asyncio
import time

from mcp.types import CallToolResult, TextContent

import mcp_bridge.openai_clients.utils as openai_utils
from mcp_bridge.logging import RequestTraceLogger


class _AsyncRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        return True


def _result(text: str = "ok") -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


def test_redis_cache_async_roundtrip() -> None:
    async def run() -> None:
        cache = openai_utils.RedisToolCache(
            url="redis://localhost:6379/0", prefix="test", ttl_seconds=60
        )
        fake = _AsyncRedis()
        cache._async_client = fake  # type: ignore[assignment]

        await cache.aput("search", {"query": "climate"}, _result())
        hit = await cache.aget("search", {"query": "climate"})

        assert hit is not None
        assert hit.content[0].text == "ok"

    asyncio.run(run())


def test_get_tool_cache_returns_a_singleton(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("MCP_BRIDGE_TOOL_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(openai_utils, "_TOOL_CACHE_SINGLETONS", {}, raising=False)

    first = openai_utils.get_tool_cache()
    second = openai_utils.get_tool_cache()

    assert first is second


def test_trace_record_does_not_block_the_loop() -> None:
    logger = RequestTraceLogger({"prompt": "x"}, "/v1/chat/completions", "POST")

    def slow_write() -> None:
        time.sleep(0.2)

    logger._write = slow_write  # type: ignore[method-assign]

    async def run() -> float:
        start = time.perf_counter()
        logger.record("test_event")
        return time.perf_counter() - start

    elapsed = asyncio.run(run())

    assert elapsed < 0.05, f"record() blocked for {elapsed:.3f}s"


def test_call_tool_signature_is_looked_up_once(monkeypatch) -> None:
    fn = openai_utils.call_tool
    openai_utils._call_tool_parameters(fn)

    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("signature was recomputed")

    monkeypatch.setattr(openai_utils.inspect, "signature", explode)

    # A cache hit must not call inspect.signature again.
    assert "timeout" in openai_utils._call_tool_parameters(fn)
