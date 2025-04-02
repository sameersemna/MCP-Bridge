from lmos_openai_types import CreateCompletionRequest
from .genericHttpxClient import get_client


async def completions(request: CreateCompletionRequest) -> dict:
    """performs a completion using the inference server"""

    response = await get_client(request).post(
        "/completions",
        json=request.model_dump(
            exclude_defaults=True, exclude_none=True, exclude_unset=True
        ),
    )
    return response.json()
