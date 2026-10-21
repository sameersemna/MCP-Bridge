from fastapi import APIRouter, Depends
from mcp_bridge.auth import get_api_key

from mcp_bridge.endpoints import router as endpointRouter
from mcp_bridge.mcpManagement import router as mcpRouter
from mcp_bridge.health import ready_router as healthReadyRouter, router as healthRouter
from mcp_bridge.mcp_server import router as mcp_server_router

secure_router = APIRouter(dependencies=[Depends(get_api_key)])

secure_router.include_router(endpointRouter)
secure_router.include_router(mcpRouter)
secure_router.include_router(mcp_server_router)
secure_router.include_router(healthReadyRouter)

public_router = APIRouter()

public_router.include_router(healthRouter)
