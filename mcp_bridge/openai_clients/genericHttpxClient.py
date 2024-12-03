from httpx import AsyncClient
from config import settings

client: AsyncClient = AsyncClient(
    base_url=settings.inference_server.base_url,
    headers={"Authorization": f"Bearer {settings.inference_server.api_key}"},
    timeout=10000
)
