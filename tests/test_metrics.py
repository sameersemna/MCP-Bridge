"""Prometheus-format metrics and the optional endpoint (P2-03)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_bridge.config import config
from mcp_bridge.metrics import MetricsRegistry, metrics
from mcp_bridge.metrics_router import router as metrics_router


def test_counter_and_summary_render() -> None:
    registry = MetricsRegistry()
    registry.increment(
        "mcp_bridge_tool_calls_total", {"tool": "search", "outcome": "ok"}
    )
    registry.observe("mcp_bridge_upstream_latency_seconds", 0.5)

    rendered = registry.render()

    assert "# TYPE mcp_bridge_tool_calls_total counter" in rendered
    assert (
        'mcp_bridge_tool_calls_total{outcome="ok",tool="search"} 1' in rendered
    )
    assert "# TYPE mcp_bridge_upstream_latency_seconds summary" in rendered
    assert "mcp_bridge_upstream_latency_seconds_count 1" in rendered
    assert "mcp_bridge_upstream_latency_seconds_sum 0.5" in rendered


def test_metrics_endpoint_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        config.telemetry, "metrics_enabled", False, raising=False
    )
    app = FastAPI()
    app.include_router(metrics_router)

    assert TestClient(app).get("/metrics").status_code == 404


def test_metrics_endpoint_renders_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        config.telemetry, "metrics_enabled", True, raising=False
    )
    metrics.reset()
    metrics.increment(
        "mcp_bridge_tool_calls_total", {"tool": "search", "outcome": "ok"}
    )
    app = FastAPI()
    app.include_router(metrics_router)

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert "mcp_bridge_tool_calls_total" in response.text
    assert response.headers["content-type"].startswith("text/plain")
