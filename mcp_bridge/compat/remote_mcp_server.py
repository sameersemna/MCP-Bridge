import asyncio
import json
import os
import sys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('local-search-tools')

@mcp.tool()
def search_web(query: str) -> str:
    """Search the web for a query using a simple fallback implementation."""
    return f"Search placeholder for: {query}"

@mcp.tool()
def fetch_page(url: str) -> str:
    """Fetch a page and return the page title or a placeholder."""
    return f"Page placeholder for: {url}"

async def main() -> None:
    await mcp.run_stdio_async()

if __name__ == '__main__':
    asyncio.run(main())
