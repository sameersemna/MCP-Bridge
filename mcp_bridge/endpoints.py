from fastapi import APIRouter

from lmos_openai_types import CreateChatCompletionRequest, CreateCompletionRequest

from openai_clients import client, completions, chat_completions

router = APIRouter(prefix="/v1")


@router.post("/completions")
async def openai_completions(request: CreateCompletionRequest):
    """Completions endpoint"""
    if request.stream:
        raise NotImplementedError("Streaming Completion is not supported")
    else:
        return await completions(request)


@router.post("/chat/completions")
async def openai_chat_completions(request: CreateChatCompletionRequest):
    """Chat Completions endpoint"""
    if request.stream:
        raise NotImplementedError("Streaming Chat Completion is not supported")
    else:
        return await chat_completions(request)


@router.get("/models")
async def models():
    """List models"""
    response = await client.get("/models")
    return response.json()
