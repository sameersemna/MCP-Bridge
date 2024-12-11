import httpx
from typing import Any

def load_config(url: str) -> dict[str, Any]:
    resp = httpx.get(str(url))
    return resp.json()