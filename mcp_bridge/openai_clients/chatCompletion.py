from lmos_openai_types import CreateChatCompletionRequest
from .genericHttpxClient import client


async def chat_completions(request: CreateChatCompletionRequest) -> dict:
    """performs a chat completion using the inference server"""

    response = await client.post(
        "/chat/completions",
        json=request.model_dump(
            exclude_defaults=True, exclude_none=True, exclude_unset=True
        ),
    )
    return response.json()
