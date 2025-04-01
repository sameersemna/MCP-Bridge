from fastapi import APIRouter, Depends, Request

from lmos_openai_types import CreateChatCompletionRequest, CreateCompletionRequest

from mcp_bridge.openai_clients import (
    client,
    completions,
    chat_completions,
    streaming_chat_completions,
)

from mcp_bridge.openapi_tags import Tag

router = APIRouter(prefix="/v1", tags=[Tag.openai])


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
        return await streaming_chat_completions(request)
    else:
        return await chat_completions(request)


@router.get("/models")
async def models(request: Request):
    """List models"""
    # Convert headers to dictionary with case normalization (lowercase)
    headers = {k.lower(): v for k, v in request.headers.items()}
    
    # List of headers to check and forward (in lowercase)
    # These headers are controlled by ENABLE_FORWARD_USER_INFO_HEADERS setting in Open-WebUI
    # See documentation: https://docs.openwebui.com/getting-started/env-configuration#enable_forward_user_info_headers
    # When enabled, Open-WebUI forwards user information (name, id, email, and role) as X-headers
    openwebui_headers = [
        "x-openwebui-user-name",
        "x-openwebui-user-id",
        "x-openwebui-user-email",
        "x-openwebui-user-role"
    ]
    
    # Check each header and set it in the client if it exists
    for header in openwebui_headers:
        if header in headers:
            client.headers[header] = headers[header]
    
    response = await client.get("/models")
    return response.json()
