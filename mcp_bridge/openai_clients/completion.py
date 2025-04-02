from fastapi import Request
from lmos_openai_types import CreateCompletionRequest
from .genericHttpxClient import get_client


async def completions(request: CreateCompletionRequest, http_request: Request) -> dict:
    """performs a completion using the inference server"""

    async with get_client(http_request) as client:
        response = await client.post(
            "/completions",
            json=request.model_dump(
                exclude_defaults=True, exclude_none=True, exclude_unset=True
            ),
        )
    return response.json()
