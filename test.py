json = """

{
    "messages": [
        {
            "content": "call fetch tool for http://example.com and return the response",
            "role": "user"
        }
    ],
    "model": "/models/Qwen2.5-Coder-32B-Instruct",
    "stream": true
}

"""

import asyncio
from httpx import AsyncClient
from httpx_sse import aconnect_sse


async def main():
    async with aconnect_sse(
        AsyncClient(), "post", "http://localhost:9090/v1/chat/completions", content=json
    ) as event_source:
        async for sse in event_source.aiter_sse():
            print(f"{sse.event=}\n{sse.data=}\n{sse.id=}\n{sse.retry=}\n")


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
