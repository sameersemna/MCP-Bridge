"""Opt-in rate/concurrency/body limits and the per-request tool cap (P2-04)."""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import mcp_bridge.openai_clients.utils as openai_utils
from mcp_bridge.config import config
from mcp_bridge.limits import LimitsMiddleware, TokenBucket, limiter


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LimitsMiddleware)

    @app.get("/ping")
    async def ping():  # type: ignore[no-untyped-def]
        return {"ok": True}

    return app


def _enable(monkeypatch, **overrides) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config.limits, "enabled", True, raising=False)
    monkeypatch.setattr(
        config.limits, "rate_limit_per_minute", 1000, raising=False
    )
    monkeypatch.setattr(
        config.limits, "max_concurrent_per_key", 10, raising=False
    )
    monkeypatch.setattr(
        config.limits, "max_body_bytes", 1024 * 1024, raising=False
    )
    for name, value in overrides.items():
        monkeypatch.setattr(config.limits, name, value, raising=False)
    limiter.reset()


def test_token_bucket_refills_over_time() -> None:
    bucket = TokenBucket(rate_per_minute=1, capacity=1)
    start = time.monotonic()

    assert bucket.allow(now=start) is True
    assert bucket.allow(now=start + 1.0) is False
    assert bucket.allow(now=start + 61.0) is True


def test_rate_limit_returns_429_with_retry_after(monkeypatch) -> None:
    _enable(monkeypatch, rate_limit_per_minute=1)
    client = TestClient(_app())

    first = client.get("/ping")
    second = client.get("/ping")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert second.json()["error"]["code"] == "rate_limited"


def test_oversized_body_is_rejected(monkeypatch) -> None:
    _enable(monkeypatch, max_body_bytes=10)
    client = TestClient(_app())

    response = client.post(
        "/ping",
        content=b"x" * 100,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_limits_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(config.limits, "enabled", False, raising=False)
    limiter.reset()
    client = TestClient(_app())

    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200


def test_call_tools_caps_concurrent_calls(monkeypatch) -> None:
    active = 0
    peak = 0

    async def fake_call_tool(name, payload, timeout=None):  # type: ignore[no-untyped-def]
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"isError": False, "content": []}

    monkeypatch.setattr(openai_utils, "call_tool", fake_call_tool)

    results = asyncio.run(
        openai_utils.call_tools(
            [(f"tool-{index}", "{}") for index in range(4)],
            max_concurrency=2,
        )
    )

    assert len(results) == 4
    assert peak == 2
