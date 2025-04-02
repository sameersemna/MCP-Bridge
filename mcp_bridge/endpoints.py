from fastapi import APIRouter, Depends, Request

from lmos_openai_types import CreateChatCompletionRequest, CreateCompletionRequest

from mcp_bridge.openai_clients import (
    get_client,
    completions,
    chat_completions,
    streaming_chat_completions,
)

from mcp_bridge.openapi_tags import Tag

router = APIRouter(prefix="/v1", tags=[Tag.openai])


@router.post("/completions")
async def openai_completions(
    request: CreateCompletionRequest, 
    http_request: Request
):
    """Completions endpoint"""
    if request.stream:
        raise NotImplementedError("Streaming Completion is not supported")
    else:
        return await completions(request, http_request)


@router.post("/chat/completions")
async def openai_chat_completions(
    request: CreateChatCompletionRequest, 
    http_request: Request
):
    """Chat Completions endpoint"""
    if request.stream:
        return await streaming_chat_completions(request, http_request)
    else:
        return await chat_completions(request, http_request)


@router.get("/models")
async def models(request: Request):
    """List models"""
    async with get_client(request) as client:
        response = await client.get("/models")
    return response.json()
