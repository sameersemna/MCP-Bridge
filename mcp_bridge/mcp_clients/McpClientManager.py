import asyncio
import json
from typing import Any, Union

from loguru import logger

try:
    from mcp import McpError, StdioServerParameters
except ImportError:  # pragma: no cover - allows tests to run without the SDK installed
    McpError = RuntimeError

    class StdioServerParameters:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

try:
    from mcpx.client.transports.docker import DockerMCPServer
except ImportError:  # pragma: no cover - fallback for environments without the SDK installed
    class DockerMCPServer:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

from mcp_bridge.config import config
from mcp_bridge.config.final import SSEMCPServer

from .DockerClient import DockerClient
from .SseClient import SseClient
from .StdioClient import StdioClient

DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS = 30.0

client_types = Union[StdioClient, SseClient, DockerClient]


def _is_disabled_server(server_config: Any) -> bool:
    if isinstance(server_config, dict):
        return bool(server_config.get("disabled"))

    disabled = getattr(server_config, "disabled", None)
    if disabled is not None:
        return bool(disabled)

    if hasattr(server_config, "model_extra") and server_config.model_extra:
        extra_disabled = server_config.model_extra.get("disabled")
        if extra_disabled is not None:
            return bool(extra_disabled)

    return False


class MCPClientManager:
    clients: dict[str, client_types] = {}
    _lock = asyncio.Lock()

    async def initialize(self):
        """Initialize the MCP Client Manager and start all clients"""

        logger.debug("Initializing MCP Client Manager")

        async with self._lock:
            self.clients.clear()
            failed_servers: list[tuple[str, str]] = []
            disabled_servers: list[str] = []
            enabled_servers: list[str] = []

            configured_disabled_servers = set(getattr(config, "disabled_mcp_servers", set()) or set())

            for server_name, server_config in config.mcp_servers.items():
                if server_name in configured_disabled_servers or _is_disabled_server(server_config):
                    disabled_servers.append(server_name)
                    logger.info(f"Skipping disabled MCP server '{server_name}'")
                    continue

                enabled_servers.append(server_name)
                try:
                    self.clients[server_name] = await self.construct_client(
                        server_name, server_config
                    )
                except Exception as exc:
                    failed_servers.append((server_name, str(exc)))
                    logger.error(
                        f"Failed to initialize MCP server '{server_name}': {exc}"
                    )

            if failed_servers:
                logger.warning(
                    "MCP client initialization completed with failures: "
                    + ", ".join(f"{name} ({reason})" for name, reason in failed_servers)
                )

            inventory = {
                "enabled": enabled_servers,
                "disabled": disabled_servers,
                "failed": [name for name, _ in failed_servers],
                "active": list(self.clients.keys()),
            }

            from mcp_bridge.health.manager import manager as health_manager

            health_manager.last_inventory = inventory

            logger.info(
                "Effective MCP server inventory: "
                + json.dumps(inventory, sort_keys=True)
            )

    async def construct_client(self, name: str, server_config: Any) -> client_types:
        logger.debug(f"Constructing client for {server_config}")

        try:
            if isinstance(server_config, StdioServerParameters):
                client = StdioClient(name, server_config)
                await client.start()
                return client

            if isinstance(server_config, SSEMCPServer):
                client = SseClient(name, server_config)  # type: ignore
                await client.start()
                return client

            if isinstance(server_config, DockerMCPServer):
                client = DockerClient(name, server_config)
                await client.start()
                return client
        except Exception as exc:
            logger.warning(f"MCP client '{name}' could not be initialized: {exc}")
            raise RuntimeError(f"Unsupported or failed MCP transport for '{name}': {exc}") from exc

        raise NotImplementedError("Client Type not supported")

    def get_client(self, server_name: str):
        return self.clients[server_name]

    def get_clients(self):
        return list(self.clients.items())

    async def get_client_from_tool(self, tool: str, timeout: float | None = None):
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS

        async def _probe(client: client_types):
            try:
                if not getattr(client, "session", None):
                    wait_for_session = getattr(client, "_wait_for_session", None)
                    if callable(wait_for_session):
                        await asyncio.wait_for(
                            wait_for_session(timeout=int(effective_timeout), http_error=False),
                            timeout=effective_timeout,
                        )
                    else:
                        list_tools = await asyncio.wait_for(
                            client.list_tools(),
                            timeout=effective_timeout,
                        )
                        for client_tool in list_tools.tools:
                            if getattr(client_tool, "name", None) == tool:
                                return client
                        return None

                if not getattr(client, "session", None):
                    return None

                list_tools = await asyncio.wait_for(
                    client.session.list_tools(),
                    timeout=effective_timeout,
                )
                for client_tool in list_tools.tools:
                    if getattr(client_tool, "name", None) == tool:
                        return client
            except asyncio.TimeoutError:
                client_name = getattr(client, "name", "unknown")
                logger.warning(f"Timed out discovering tools for client '{client_name}'")
                return None
            except Exception as exc:
                client_name = getattr(client, "name", "unknown")
                logger.debug(f"Client '{client_name}' could not be resolved for tool '{tool}': {exc}")
                return None

            return None

        clients = [client for _, client in self.get_clients()]
        if not clients:
            return None

        probe_tasks = [asyncio.create_task(_probe(client)) for client in clients]
        try:
            done, pending = await asyncio.wait(
                probe_tasks,
                timeout=effective_timeout,
                return_when=asyncio.ALL_COMPLETED,
            )
        except Exception:
            for task in probe_tasks:
                task.cancel()
            raise

        for task in done:
            result = task.result()
            if result is not None:
                for pending_task in pending:
                    pending_task.cancel()
                return result

        for task in pending:
            task.cancel()

        return None

    async def get_client_from_prompt(self, prompt: str, timeout: float | None = None):
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS

        for _, client in self.get_clients():
            try:
                if not getattr(client, "session", None):
                    wait_for_session = getattr(client, "_wait_for_session", None)
                    if callable(wait_for_session):
                        await asyncio.wait_for(
                            wait_for_session(timeout=int(effective_timeout), http_error=False),
                            timeout=effective_timeout,
                        )
                    else:
                        continue

                if not getattr(client, "session", None):
                    continue

                list_prompts = await asyncio.wait_for(
                    client.session.list_prompts(),
                    timeout=effective_timeout,
                )
                for client_prompt in list_prompts.prompts:
                    if client_prompt.name == prompt:
                        return client
            except asyncio.TimeoutError:
                client_name = getattr(client, "name", "unknown")
                logger.warning(f"Timed out discovering prompts for client '{client_name}'")
                continue
            except Exception:
                continue

        return None


ClientManager = MCPClientManager()
