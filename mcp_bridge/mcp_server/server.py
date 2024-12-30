from mcp.server import Server, NotificationOptions, InitializationOptions
from mcp import types
from pydantic import AnyUrl
from mcp_clients.McpClientManager import ClientManager
from loguru import logger

__all__ = ["server", "options"]

server = Server("MCP-Bridge")

## list functions


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    prompts = []
    for name, client in ClientManager.get_clients():
        client_prompts = await client.list_prompts()
        prompts.extend(client_prompts.prompts)
    return prompts


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    resources = []
    for name, client in ClientManager.get_clients():
        try:
            client_resources = await client.list_resources()
            resources.extend(client_resources.resources)
        except Exception as e:
            logger.error(f"Error listing resources for {name}: {e}")
    return resources


@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return []


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = []
    for name, client in ClientManager.get_clients():
        client_tools = await client.list_tools()
        tools.extend(client_tools.tools)
    return tools


## get functions


@server.get_prompt()
async def get_prompt(name: str, args: dict[str, str]) -> types.GetPromptResult:
    client = await ClientManager.get_client_from_prompt(name)
    return await client.get_prompt(name, args)


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> str:
    pass


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    client = await ClientManager.get_client_from_tool(name)
    return (await client.call_tool(name, arguments)).content


# options

options = InitializationOptions(
    server_name="MCP-Bridge",
    server_version="0.1.0",
    capabilities=server.get_capabilities(
        notification_options=NotificationOptions(),
        experimental_capabilities={},
    ),
)
