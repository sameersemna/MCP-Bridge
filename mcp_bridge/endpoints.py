import httpx
from fastapi import APIRouter
from config import settings

router = APIRouter(prefix="/v1")


@router.get("/completions")
async def completions():
    """Completions endpoint"""


@router.get("/chat/completions")
async def chat_completions():
    """Chat Completions endpoint"""


@router.get("/models")
async def models():
    """List models"""
    async with httpx.AsyncClient(
        base_url=settings.inference_server.base_url,
        headers={"Authorization": f"Bearer {settings.inference_server.api_key}"},
    ) as client:
        response = await client.get("/models")
        return response.json()
