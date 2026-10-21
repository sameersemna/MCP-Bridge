import asyncio
import base64
from typing import Any

from mcp import types
from mcp.server import Server, NotificationOptions
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.models import InitializationOptions
from pydantic import AnyUrl
from mcp_bridge import __version__
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from loguru import logger


def _normalize_arguments(arguments: dict | None) -> dict[str, object]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise TypeError("Arguments must be a JSON object")
    return {str(key): value for key, value in arguments.items()}

__all__ = ["server", "options"]

server = Server("MCP-Bridge")

## list functions


async def _prompts_for(name: str, client: Any) -> list[types.Prompt]:
    try:
        result = await client.list_prompts()
        return list(getattr(result, "prompts", []) or [])
    except Exception as e:
        logger.error(f"Error listing prompts for {name}: {e}")
        return []


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    clients = [
        (name, client)
        for name, client in ClientManager.get_clients()
        if client is not None
    ]
    batches = await asyncio.gather(
        *(_prompts_for(name, client) for name, client in clients)
    )
    return [prompt for batch in batches for prompt in batch]


async def _resources_for(name: str, client: Any) -> list[types.Resource]:
    try:
        result = await client.list_resources()
        return list(getattr(result, "resources", []) or [])
    except Exception as e:
        logger.error(f"Error listing resources for {name}: {e}")
        return []


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    clients = [
        (name, client)
        for name, client in ClientManager.get_clients()
        if client is not None
    ]
    batches = await asyncio.gather(
        *(_resources_for(name, client) for name, client in clients)
    )
    return [resource for batch in batches for resource in batch]


@server.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return []


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    # Read the warm registry instead of issuing one tools/list per client.
    await ClientManager.refresh_tool_registry()
    tools: list[types.Tool] = []
    for entry in ClientManager.registry.all():
        name = entry.exposed or entry.name
        try:
            tools.append(
                types.Tool(
                    name=name,
                    description=getattr(entry.tool, "description", None),
                    inputSchema=getattr(entry.tool, "inputSchema", {}) or {},
                )
            )
        except Exception:
            tools.append(entry.tool)
    return tools


## get functions


@server.get_prompt()
async def get_prompt(name: str, args: dict[str, str] | None) -> types.GetPromptResult:
    client = await ClientManager.get_client_from_prompt(name)

    # if client is None, then we cannot get the prompt
    if client is None:
        raise Exception(f"Prompt '{name}' not found")

    normalized_args = _normalize_arguments(args)
    result = await client.get_prompt(name, normalized_args)
    if result is None:
        raise Exception(f"Prompt '{name}' not found")

    return result


def _resource_contents(resource: Any) -> ReadResourceContents:
    """Return content with its MIME type, for text or blob resources.

    Deriving from the resource type (rather than matching ``text/plain``) lets
    custom types such as ``application/json`` and ``image/png`` through, and
    preserves the advertised MIME type instead of rejecting it.
    """
    if isinstance(resource, types.TextResourceContents):
        if not isinstance(resource.text, str):
            raise TypeError("text resource has no text content")
        return ReadResourceContents(resource.text, resource.mimeType or "text/plain")

    if isinstance(resource, types.BlobResourceContents):
        blob = resource.blob
        if isinstance(blob, bytes):
            data = blob
        elif isinstance(blob, str):
            data = base64.b64decode(blob)
        else:
            raise TypeError("blob resource has no blob content")
        return ReadResourceContents(
            data, resource.mimeType or "application/octet-stream"
        )

    mime = getattr(resource, "mimeType", "") or ""
    if mime.startswith("text/"):
        return ReadResourceContents(str(getattr(resource, "text", "")), mime)
    raise ValueError(f"Unsupported resource type: {mime or type(resource).__name__}")


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    for name, client in ClientManager.get_clients():
        try:
            client_resources = await client.list_resources()
            if str(uri) in map(lambda x: str(x.uri), client_resources.resources):
                response = await client.read_resource(uri)
                return [_resource_contents(resource) for resource in response]
        except Exception as e:
            logger.error(f"Error reading resources for {name}: {e}")

    raise Exception(f"Resource '{uri}' not found")


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    # Resolve first: the caller may use an alias or a normalized spelling, but
    # the downstream server only knows its canonical tool name.
    resolved = await ClientManager.resolve_tool(name)
    if resolved is None:
        raise Exception(f"Tool '{name}' not found")

    client, resolved_name = resolved
    normalized_arguments = _normalize_arguments(arguments)
    return (await client.call_tool(resolved_name, normalized_arguments)).content


# options

options = InitializationOptions(
    server_name="MCP-Bridge",
    server_version=__version__,
    capabilities=server.get_capabilities(
        notification_options=NotificationOptions(),
        experimental_capabilities={},
    ),
)
