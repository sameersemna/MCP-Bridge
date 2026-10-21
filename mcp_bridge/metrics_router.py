"""Optional Prometheus text endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from mcp_bridge.config import config
from mcp_bridge.metrics import metrics
from mcp_bridge.openapi_tags import Tag

router = APIRouter(tags=[Tag.health])


@router.get("/metrics", response_class=PlainTextResponse)
async def get_metrics() -> PlainTextResponse:
    if not getattr(config.telemetry, "metrics_enabled", False):
        raise HTTPException(status_code=404, detail="Metrics are disabled")
    return PlainTextResponse(
        metrics.render(), media_type="text/plain; version=0.0.4"
    )
