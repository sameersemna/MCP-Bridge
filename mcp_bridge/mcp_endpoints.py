from fastapi import APIRouter
from mcp_clients.McpClientManager import ClientManager
from mcp.types import ListToolsResult
from openapi_tags import Tag

router = APIRouter(prefix="/mcp", tags=[Tag.mcp_management])


@router.get("/tools")
async def get_tools() -> dict[str, ListToolsResult]:
    """Get all tools from all MCP clients"""

    tools = {}

    for name, client in ClientManager.get_clients():
        tools[name] = await client.session.list_tools()

    return tools

@router.get("/servers")
async def get_servers() -> list[str]:
    """List all MCP servers"""

    servers = []

    for name, client in ClientManager.get_clients():
        servers.append(name)

    return servers