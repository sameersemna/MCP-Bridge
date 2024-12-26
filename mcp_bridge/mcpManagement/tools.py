from fastapi import APIRouter, HTTPException
from mcp_clients.McpClientManager import ClientManager
from mcp.types import ListToolsResult

router = APIRouter(prefix="/tools")

@router.get("")
async def get_tools() -> dict[str, ListToolsResult]:
    """Get all tools from all MCP clients"""

    tools = {}

    for name, client in ClientManager.get_clients():
        tools[name] = await client.list_tools()

    return tools
