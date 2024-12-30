from fastapi import APIRouter
from .sse import router as sse_router

__all__ = ["router"]

router = APIRouter(prefix="/mcp-server")
router.include_router(sse_router)
