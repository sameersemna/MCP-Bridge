import asyncio
import difflib
import json
from typing import Any, Union
from urllib.parse import urlparse

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
from .SseClient import HttpClient, SseClient
from .StdioClient import StdioClient

DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS = 10.0

client_types = Union[StdioClient, SseClient, HttpClient, DockerClient]


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

    @staticmethod
    def _normalize_tool_name(tool: str) -> str:
        normalized = tool.strip().lower().replace("-", "_")
        return normalized

    @staticmethod
    def _get_client_class(server_config: Any) -> type[client_types]:
        if isinstance(server_config, StdioServerParameters):
            return StdioClient

        if isinstance(server_config, SSEMCPServer):
            transport_type = getattr(server_config, "type", None)
            url = getattr(server_config, "url", "")
            parsed_url = urlparse(url)
            path = (parsed_url.path or "").rstrip("/")
            is_sse_endpoint = path == "/sse" or path.endswith("/sse")

            if transport_type == "sse" or is_sse_endpoint:
                return SseClient
            if transport_type == "http":
                return HttpClient
            return HttpClient

        if isinstance(server_config, DockerMCPServer):
            return DockerClient

        raise NotImplementedError("Client Type not supported")

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
            client_class = self._get_client_class(server_config)
            if client_class is StdioClient:
                client = client_class(name, server_config)
                await client.start()
                return client

            if client_class in {SseClient, HttpClient}:
                client = client_class(name, server_config)  # type: ignore[arg-type]
                await client.start()
                return client

            if client_class is DockerClient:
                client = client_class(name, server_config)
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
        resolved = await self.resolve_tool(tool, timeout=timeout)
        if resolved is None:
            return None
        client, _ = resolved
        return client

    async def _list_client_tools(self, client: client_types, timeout: float) -> list[Any]:
        """Return the list of tools exposed by a client, or ``[]`` on failure.

        Handles both clients that expose a ``session`` (with ``list_tools``)
        and those that expose a top-level ``list_tools`` method, waiting for
        the session to become ready when needed.
        """
        try:
            if not getattr(client, "session", None):
                wait_for_session = getattr(client, "_wait_for_session", None)
                if callable(wait_for_session):
                    configured_request_timeout = None
                    config = getattr(client, "config", None)
                    request_timeout = getattr(config, "requestTimeout", None)
                    if request_timeout is not None:
                        configured_request_timeout = float(request_timeout) / 1000.0

                    wait_timeout = float(timeout)
                    if configured_request_timeout is not None:
                        wait_timeout = max(wait_timeout, configured_request_timeout)

                    await wait_for_session(timeout=int(wait_timeout), http_error=False)
                else:
                    list_tools = await asyncio.wait_for(client.list_tools(), timeout=timeout)
                    return list(getattr(list_tools, "tools", []) or [])

            if not getattr(client, "session", None):
                return []

            list_tools = await asyncio.wait_for(client.session.list_tools(), timeout=timeout)
            return list(getattr(list_tools, "tools", []) or [])
        except asyncio.TimeoutError:
            client_name = getattr(client, "name", "unknown")
            logger.warning(f"Timed out discovering tools for client '{client_name}'")
            return []
        except Exception as exc:
            client_name = getattr(client, "name", "unknown")
            logger.debug(f"Client '{client_name}' could not be resolved for tools: {exc}")
            return []

    async def resolve_tool(self, tool: str, timeout: float | None = None):
        """Resolve a (possibly loosely-named) tool call to its owning client and
        the exact tool name the server expects.

        LLMs frequently call tools by a name that differs from the registered
        name (e.g. ``google-search`` vs ``google_search``, or a server name
        instead of a tool name). This normalizes separators and returns the
        canonical tool name so the downstream ``session.call_tool`` uses the
        name the server actually recognizes.

        If the called name matches no tool but matches a *server* name, and
        that server exposes exactly one tool, the call is dispatched to that
        tool directly (the common "called the server instead of the tool" case).

        Returns ``(client, actual_tool_name)`` or ``None`` if no client exposes
        a matching tool.
        """
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS

        normalized_tool = self._normalize_tool_name(tool)

        async def _probe(client: client_types):
            client_tools = await self._list_client_tools(client, effective_timeout)
            for client_tool in client_tools:
                if self._normalize_tool_name(getattr(client_tool, "name", "")) == normalized_tool:
                    return (client, getattr(client_tool, "name", tool))
            return None

        clients = [client for _, client in self.get_clients()]
        if not clients:
            return None

        probe_tasks = [asyncio.create_task(_probe(client)) for client in clients]
        try:
            deadline = asyncio.get_running_loop().time() + effective_timeout
            while probe_tasks:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break

                done, pending = await asyncio.wait(
                    probe_tasks,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    probe_tasks.remove(task)
                    result = task.result()
                    if result is not None:
                        for pending_task in pending:
                            pending_task.cancel()
                        return result

                if not pending:
                    break

                probe_tasks = list(pending)
        except Exception:
            for task in probe_tasks:
                task.cancel()
            raise

        for task in probe_tasks:
            task.cancel()

        # Server-name fallback: the called name matched no tool, but it may be
        # a *server* name. If that server exposes exactly one tool, dispatch to
        # it directly so the common "called the server" case succeeds silently.
        for client in clients:
            client_name = getattr(client, "name", "")
            if client_name and self._normalize_tool_name(client_name) == normalized_tool:
                client_tools = await self._list_client_tools(client, effective_timeout)
                if len(client_tools) == 1:
                    return (client, getattr(client_tools[0], "name", tool))
                # Multiple tools on the matched server: cannot disambiguate
                # reliably, so fall through to the corrective error path.
                break

        return None

    async def suggest_tools(self, tool: str, timeout: float | None = None) -> list[str]:
        """Return a list of registered tool names that are close matches to the
        given (misnamed) tool call, for use in a corrective error message.

        If the called name matches a *server* name, that server's tools are
        returned first (the model likely meant one of them). Otherwise fuzzy
        matching (``difflib``) is used against every tool name exposed by every
        client, so the model can see what it should have called.
        """
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS

        normalized_tool = self._normalize_tool_name(tool)

        # Server-name match: the model likely meant a tool on this server.
        for _, client in self.get_clients():
            client_name = getattr(client, "name", "")
            if client_name and self._normalize_tool_name(client_name) == normalized_tool:
                client_tools = await self._list_client_tools(client, effective_timeout)
                names = [getattr(t, "name", "") for t in client_tools]
                return [n for n in names if n]

        all_tool_names: list[str] = []
        for _, client in self.get_clients():
            client_tools = await self._list_client_tools(client, effective_timeout)
            for client_tool in client_tools:
                name = getattr(client_tool, "name", "")
                if name:
                    all_tool_names.append(name)

        if not all_tool_names:
            return []

        matches = difflib.get_close_matches(normalized_tool, [self._normalize_tool_name(n) for n in all_tool_names], n=5, cutoff=0.4)
        normalized_to_original = {self._normalize_tool_name(n): n for n in all_tool_names}
        return [normalized_to_original[m] for m in matches]

    async def get_client_from_prompt(self, prompt: str, timeout: float | None = None):
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS

        for _, client in self.get_clients():
            try:
                if not getattr(client, "session", None):
                    wait_for_session = getattr(client, "_wait_for_session", None)
                    if callable(wait_for_session):
                        configured_request_timeout = None
                        config = getattr(client, "config", None)
                        request_timeout = getattr(config, "requestTimeout", None)
                        if request_timeout is not None:
                            configured_request_timeout = float(request_timeout) / 1000.0

                        wait_timeout = float(effective_timeout)
                        if configured_request_timeout is not None:
                            wait_timeout = max(wait_timeout, configured_request_timeout)

                        await wait_for_session(timeout=int(wait_timeout), http_error=False)
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
