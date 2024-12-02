from lmos_openai_types import CreateChatCompletionRequest
from .genericHttpxClient import client
from mcp_clients.McpClientManager import ClientManager
from tool_mappers import mcp2openai

async def chat_completions(request: CreateChatCompletionRequest) -> dict:
    """performs a chat completion using the inference server"""

    request.tools = []

    for _, session in ClientManager.get_clients():
        tools = await session.session.list_tools()
        for tool in tools.tools:
            request.tools.append(mcp2openai(tool))

    response = await client.post(
        "/chat/completions",
        json=request.model_dump(
            exclude_defaults=True, exclude_none=True, exclude_unset=True
        ),
    )
    return response.json()
