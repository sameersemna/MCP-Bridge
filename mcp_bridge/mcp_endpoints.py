from fastapi import APIRouter
from mcp_clients.McpClientManager import ClientManager

router = APIRouter(
    prefix="/mcp"
)

@router.get("/tools")
async def get_tools() :
    tools = {}

    for name, client in  ClientManager.get_clients() :
        tools[name] = await client.session.list_tools()

    return tools