from fastapi import APIRouter
from fastapi.responses import JSONResponse
from .types import HealthCheckResponse
from .manager import manager
from mcp_bridge.openapi_tags import Tag

router = APIRouter(tags=[Tag.health])

# Mounted on the authenticated router. ``get_api_key`` returns immediately when
# auth is disabled, so readiness follows the operator's auth setting: public
# when auth is off, protected when it is on.
ready_router = APIRouter(tags=[Tag.health])


async def _health_response():
    """Build the detailed health response (500 when unhealthy)."""
    healthy = manager.is_healthy()

    if not healthy:
        response = HealthCheckResponse(
            status="error",
            unhealthy_events=manager.get_unhealthy_events(),
            mcp_servers=manager.get_mcp_server_health(),
            mcp_inventory=manager.get_mcp_inventory(),
        )
        return JSONResponse(content=response.model_dump(), status_code=500)

    return HealthCheckResponse(
        status="ok",
        unhealthy_events=[],
        mcp_servers=manager.get_mcp_server_health(),
        mcp_inventory=manager.get_mcp_inventory(),
    )


@router.get("/health", response_model=HealthCheckResponse)
async def health():
    """Health check endpoint (unchanged public behaviour)."""
    return await _health_response()


@router.get("/health/live")
async def health_live():
    """Minimal liveness probe: the process is running and serving HTTP."""
    return {"status": "ok"}


@ready_router.get("/health/ready", response_model=HealthCheckResponse)
async def health_ready():
    """Detailed readiness, protected by the API key when auth is enabled."""
    return await _health_response()
