import httpx
from fastapi import APIRouter
from config import settings

from lmos_openai_types import CreateChatCompletionRequest, CreateCompletionRequest

router = APIRouter(prefix="/v1")


@router.post("/completions")
async def completions(request: CreateCompletionRequest):
    """Completions endpoint"""
    if request.stream:
        raise NotImplementedError("Streaming Completion is not supported")
    else:
        raise NotImplementedError("Non-streaming Completion is not supported")


@router.post("/chat/completions")
async def chat_completions(request: CreateChatCompletionRequest):
    """Chat Completions endpoint"""
    if request.stream:
        raise NotImplementedError("Streaming Chat Completion is not supported")
    else:
        raise NotImplementedError("Non-streaming Chat Completion is not supported")


@router.get("/models")
async def models():
    """List models"""
    async with httpx.AsyncClient(
        base_url=settings.inference_server.base_url,
        headers={"Authorization": f"Bearer {settings.inference_server.api_key}"},
    ) as client:
        response = await client.get("/models")
        return response.json()
