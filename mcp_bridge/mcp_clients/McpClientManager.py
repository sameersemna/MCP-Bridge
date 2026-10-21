import asyncio
import difflib
import json
import os
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
from mcp_bridge.logging import redact_sensitive_data

from .DockerClient import DockerClient
from .SseClient import HttpClient, SseClient
from .StdioClient import StdioClient
from .ToolRegistry import RegisteredTool, ToolRegistry, normalize_tool_name

DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS = 10.0

# How long shutdown waits for all MCP clients to stop. The budget is shared:
# clients stop concurrently, so a hung transport costs at most this long.
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _shutdown_timeout_seconds() -> float:
    """Read the shutdown budget from the environment, falling back sanely."""
    raw = os.getenv("MCP_BRIDGE_SHUTDOWN_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_SHUTDOWN_TIMEOUT_SECONDS

client_types = Union[StdioClient, SseClient, HttpClient, DockerClient]

# Curated aliases for tool names that LLMs commonly use to mean a specific
# registered tool. Keys are normalized (lowercase, ``-`` -> ``_``); values are
# ordered candidate normalized names, most-specific first. Resolution only
# succeeds when a candidate is *actually registered* for the current server
# set, so this map is always safe to apply -- on a server set where the
# candidate does not exist, resolution simply falls through to the corrective
# error path as before.
#
# This exists because models routinely invent a generic name (``web_search``)
# for a tool registered under a more specific name (``web_search_exa``). Without
# it the call fails, the model is told "not a registered tool", and it often
# calls the same invented name again -- burning tool-loop turns for nothing.
# Observed in a real run: ``no MCP client found for tool 'web_search'`` four
# times in a single batch, then again on the next turn.
_TOOL_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    # Generic web-search names -> the specific search tools this bridge exposes.
    "web_search": ("web_search_exa", "search", "google_search", "you_search"),
    "websearch": ("web_search_exa", "search", "google_search", "you_search"),
    "search_web": ("web_search_exa", "search", "google_search", "you_search"),
    "internet_search": ("web_search_exa", "search", "google_search"),
    "online_search": ("web_search_exa", "search", "google_search"),
    "google": ("google_search",),
    "google_web_search": ("google_search",),
    # Generic fetch names -> the specific fetch tools.
    "web_fetch": ("web_fetch_exa", "fetch_content", "fetch"),
    "webfetch": ("web_fetch_exa", "fetch_content", "fetch"),
    "fetch_url": ("fetch_content", "fetch", "web_fetch_exa"),
    "read_url": ("fetch_content", "fetch", "read_pdf"),
    "get_url": ("fetch_content", "fetch"),
    "open_url": ("fetch_content", "fetch"),
    "download": ("fetch_content", "fetch", "download_paper"),
    # PDF reading.
    "pdf_read": ("read_pdf",),
    "readpdf": ("read_pdf",),
    # Reasoning.
    "think": ("sequentialthinking",),
    "reasoning": ("sequentialthinking",),
    "sequential_thinking": ("sequentialthinking",),
    # Paper search.
    "arxiv_search": ("search_papers",),
    "paper_search": ("search_papers",),
    "papers": ("search_papers", "list_papers"),
}

# CLI flags (e.g. `--password`, `--api-key`) whose *following* arg value is a
# secret. `redact_sensitive_data` only redacts dict values by key name, which
# covers `env` and `auth`, but a value passed as a bare CLI arg inside
# `args` (e.g. redis-mcp-server's `--password <value>`) has no key to match
# against, so it needs this separate pass.
_SENSITIVE_ARG_FLAG_KEYWORDS = ("password", "token", "secret", "key", "authorization")


def _redact_server_config_for_logging(server_config: Any) -> Any:
    """Best-effort redaction of a server config for a debug log line.

    Falls back to the original object on any unexpected shape so that a
    logging helper can never break client construction.
    """
    try:
        if hasattr(server_config, "model_dump"):
            data = server_config.model_dump()
        elif isinstance(server_config, dict):
            data = dict(server_config)
        elif hasattr(server_config, "__dict__"):
            data = dict(vars(server_config))
        else:
            return server_config

        data = redact_sensitive_data(data)

        # A header's name does not reveal whether its value is secret (tokens
        # are routinely sent in custom headers), so redact every value. For
        # `auth`, keep only the non-secret `type` discriminator.
        headers = data.get("headers")
        if isinstance(headers, dict):
            data["headers"] = {str(key): "[REDACTED]" for key in headers}
        auth = data.get("auth")
        if isinstance(auth, dict):
            data["auth"] = {
                str(key): (value if str(key).lower() == "type" else "[REDACTED]")
                for key, value in auth.items()
            }

        args = data.get("args")
        if isinstance(args, list):
            redacted_args = list(args)
            for i, item in enumerate(redacted_args[:-1]):
                if (
                    isinstance(item, str)
                    and item.startswith("-")
                    and any(keyword in item.lower() for keyword in _SENSITIVE_ARG_FLAG_KEYWORDS)
                ):
                    redacted_args[i + 1] = "[REDACTED]"
            data["args"] = redacted_args

        return data
    except Exception:
        return server_config


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

    def __init__(self) -> None:
        # One process-wide view of the downstream tool set. Resolution,
        # suggestions, discovery and the aggregated server all read this
        # instead of re-running tools/list per call.
        self.registry = ToolRegistry()

    @staticmethod
    def _normalize_tool_name(tool: str) -> str:
        return normalize_tool_name(tool)

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

            enabled: list[tuple[str, Any]] = []
            for server_name, server_config in config.mcp_servers.items():
                if server_name in configured_disabled_servers or _is_disabled_server(server_config):
                    disabled_servers.append(server_name)
                    logger.info(f"Skipping disabled MCP server '{server_name}'")
                    continue

                enabled_servers.append(server_name)
                enabled.append((server_name, server_config))

            # Start every server concurrently: one slow server (a remote SSE
            # handshake, an npx download) must not delay all the others.
            # ``return_exceptions`` keeps a single failure isolated.
            results = await asyncio.gather(
                *(
                    self.construct_client(server_name, server_config)
                    for server_name, server_config in enabled
                ),
                return_exceptions=True,
            )

            for (server_name, _), result in zip(enabled, results):
                if isinstance(result, BaseException):
                    failed_servers.append((server_name, str(result)))
                    logger.error(
                        f"Failed to initialize MCP server '{server_name}': {result}"
                    )
                else:
                    self.clients[server_name] = result

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

    async def notify_cancelled(self, reason: str = "client disconnected") -> None:
        """Tell every downstream client to cancel its in-flight request."""
        await asyncio.gather(
            *(
                client.notify_cancelled(reason)
                for _, client in self.get_clients()
                if getattr(client, "notify_cancelled", None) is not None
            ),
            return_exceptions=True,
        )

    async def shutdown(self, timeout: float | None = None) -> None:
        """Stop every MCP client concurrently and clear the registry.

        This is terminal: call it from the application lifespan on exit. Stdio
        clients own a child process, so skipping ``stop()`` orphans it. Every
        client gets the same deadline rather than a serial budget, so one hung
        transport cannot delay the rest beyond ``timeout`` seconds.
        """
        if timeout is None:
            timeout = _shutdown_timeout_seconds()

        async with self._lock:
            clients = list(self.clients.items())
            self.clients.clear()

        if not clients:
            return

        logger.info(f"Stopping {len(clients)} MCP client(s) (timeout {timeout:.1f}s)")

        async def _stop(name: str, client: client_types) -> None:
            stop = getattr(client, "stop", None)
            if not callable(stop):
                return
            try:
                await asyncio.wait_for(stop(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Timed out after {timeout:.1f}s stopping MCP client '{name}'; "
                    "its transport may still be open"
                )
            except Exception as exc:
                logger.warning(f"Failed to stop MCP client '{name}': {exc}")

        await asyncio.gather(*(_stop(name, client) for name, client in clients))

    async def construct_client(self, name: str, server_config: Any) -> client_types:
        logger.debug(f"Constructing client for {_redact_server_config_for_logging(server_config)}")

        try:
            client_class = self._get_client_class(server_config)
            if client_class is StdioClient:
                client = client_class(name, server_config)
                client.on_tools_changed = self.registry.invalidate
                await client.start()
                self._apply_concurrency_limit(name, client)
                return client

            if client_class in {SseClient, HttpClient}:
                client = client_class(name, server_config)  # type: ignore[arg-type]
                client.on_tools_changed = self.registry.invalidate
                await client.start()
                self._apply_concurrency_limit(name, client)
                return client

            if client_class is DockerClient:
                client = client_class(name, server_config)
                client.on_tools_changed = self.registry.invalidate
                await client.start()
                self._apply_concurrency_limit(name, client)
                return client
        except Exception as exc:
            logger.warning(f"MCP client '{name}' could not be initialized: {exc}")
            raise RuntimeError(f"Unsupported or failed MCP transport for '{name}': {exc}") from exc

        raise NotImplementedError("Client Type not supported")

    def _apply_concurrency_limit(self, name: str, client: client_types) -> None:
        """Apply the per-server ``max_concurrent_calls`` limit to a client.

        Reads the limit from ``config.mcp_server_concurrency`` (populated from
        each server's ``"max_concurrent_calls"`` config key). A missing entry
        leaves the client at its default of 1 (serialized), preserving the
        historical behavior.
        """
        limit = getattr(config, "mcp_server_concurrency", {}).get(name, 1)
        setter = getattr(client, "set_max_concurrent_calls", None)
        if callable(setter):
            setter(limit)

    def get_client(self, server_name: str):
        # Returning None (instead of raising KeyError) lets the management
        # routers turn a missing server into their intended 404 rather than a
        # 500 from an uncaught exception.
        return self.clients.get(server_name)

    def get_clients(self):
        return list(self.clients.items())

    async def get_client_from_tool(self, tool: str, timeout: float | None = None):
        resolved = await self.resolve_tool(tool, timeout=timeout)
        if resolved is None:
            return None
        client, _ = resolved
        return client

    @staticmethod
    def _remember_tool_annotations(client: Any, tools: Any) -> None:
        """Capture declared annotations so call_tool can judge retry safety."""
        recorder = getattr(client, "remember_tool_annotations", None)
        if callable(recorder):
            recorder(tools)

    async def _list_client_tools(
        self, client: client_types, timeout: float, *, wait_for_ready: bool = True
    ) -> list[Any]:
        """Return the list of tools exposed by a client, or ``[]`` on failure.

        Handles both clients that expose a ``session`` (with ``list_tools``)
        and those that expose a top-level ``list_tools`` method, waiting for
        the session to become ready when needed.
        """
        try:
            if not getattr(client, "session", None):
                wait_for_session = getattr(client, "_wait_for_session", None)
                if callable(wait_for_session):
                    # Discovery must not block on a server whose maintainer has
                    # not established a session yet: the registry retries it on
                    # a later resolution. Resolution still gets the servers
                    # that are ready immediately.
                    if not wait_for_ready:
                        return []
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
                    client_tools = list(getattr(list_tools, "tools", []) or [])
                    self._remember_tool_annotations(client, client_tools)
                    return client_tools

            if not getattr(client, "session", None):
                return []

            list_tools = await asyncio.wait_for(client.session.list_tools(), timeout=timeout)  # type: ignore[union-attr]
            client_tools = list(getattr(list_tools, "tools", []) or [])
            self._remember_tool_annotations(client, client_tools)
            return client_tools
        except asyncio.TimeoutError:
            client_name = getattr(client, "name", "unknown")
            logger.warning(f"Timed out discovering tools for client '{client_name}'")
            return []
        except Exception as exc:
            client_name = getattr(client, "name", "unknown")
            logger.debug(f"Client '{client_name}' could not be resolved for tools: {exc}")
            return []

    def _sync_registry_servers(self) -> None:
        """Drop cached tools when the configured client set changes.

        Without this, a registry warmed for one client set would keep serving
        those tools after clients are replaced (notably between tests, but also
        after a configuration reload).
        """
        current = [name for name, _ in self.get_clients()]
        if current != self.registry.server_order():
            self.registry.set_server_order(current)
            self.registry.invalidate()

    async def _discover_server(
        self, name: str, client: client_types, timeout: float
    ) -> tuple[str, list[RegisteredTool]]:
        """Discover one server, never waiting for an unready session."""
        tools = await self._list_client_tools(client, timeout, wait_for_ready=False)
        annotations = getattr(client, "_tool_annotations", {}) or {}
        entries: list[RegisteredTool] = []
        for tool in tools:
            tool_name = getattr(tool, "name", "")
            if not tool_name:
                continue
            entries.append(
                RegisteredTool(
                    server=name,
                    name=tool_name,
                    tool=tool,
                    annotations=annotations.get(tool_name, {}),
                )
            )
        return name, entries

    async def _refresh_until_match(
        self, normalized_tool: str | None, timeout: float
    ) -> None:
        """Discover stale servers, stopping as soon as a match is found.

        ``normalized_tool=None`` means "fill in every stale server" (used on the
        error path for suggestions). Slow servers are cancelled once the match
        is known, so one unresponsive server cannot delay resolution.
        """
        self._sync_registry_servers()
        async with self.registry.lock:
            pending = [
                (name, client)
                for name, client in self.get_clients()
                if self.registry.is_server_stale(name)
            ]
            if not pending:
                return

            tasks = {
                asyncio.create_task(self._discover_server(name, client, timeout)): name
                for name, client in pending
            }
            try:
                for completed in asyncio.as_completed(tasks, timeout=timeout):
                    try:
                        name, entries = await completed
                    except Exception as exc:
                        logger.debug(f"tool discovery failed for a server: {exc!r}")
                        continue
                    self.registry.merge_server(name, entries)
                    if (
                        normalized_tool is not None
                        and self.registry.get(normalized_tool) is not None
                    ):
                        break
            except asyncio.TimeoutError:
                logger.debug("tool registry refresh hit its time budget")
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def refresh_tool_registry(
        self, *, force: bool = False, timeout: float | None = None
    ) -> None:
        """Rebuild the whole registry, skipping sessions that are not ready."""
        effective_timeout = (
            float(timeout) if timeout is not None else DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
        )
        self._sync_registry_servers()
        if not force and not self.registry.is_stale():
            return

        async with self.registry.lock:
            if not force and not self.registry.is_stale():
                return
            results = await asyncio.gather(
                *(
                    self._discover_server(name, client, effective_timeout)
                    for name, client in self.get_clients()
                ),
                return_exceptions=True,
            )
            entries: list[RegisteredTool] = []
            for result in results:
                if isinstance(result, BaseException):
                    logger.debug(f"tool registry refresh failed for a server: {result!r}")
                    continue
                _, server_entries = result
                entries.extend(server_entries)
            self.registry.replace(entries)

    async def resolve_tool(self, tool: str, timeout: float | None = None):
        """Resolve a (possibly loosely-named) tool call to its owning client.

        Resolution reads the warm registry, so it makes no ``tools/list`` RPCs
        once discovery has run. Aliases and the single-tool server-name
        fallback are resolved from registry entries too. The winner is stable
        because the registry is built in config order.
        """
        normalized_tool = self._normalize_tool_name(tool)
        clients = dict(self.get_clients())
        if not clients:
            return None

        self._sync_registry_servers()
        effective_timeout = (
            float(timeout) if timeout is not None else DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
        )

        def _from_entry(entry: RegisteredTool):
            client = clients.get(entry.server)
            if client is None:
                return None
            return (client, entry.name)

        entry = self.registry.get(normalized_tool)
        if entry is not None and not self.registry.is_server_stale(entry.server):
            resolved = _from_entry(entry)
            if resolved is not None:
                return resolved

        # Discover until the requested tool is found; a slow server is
        # cancelled once a match exists.
        await self._refresh_until_match(normalized_tool, effective_timeout)
        entry = self.registry.get(normalized_tool)
        if entry is not None:
            resolved = _from_entry(entry)
            if resolved is not None:
                return resolved

        # Not found: fill in the remaining servers before trying aliases and
        # the server-name fallback (error path, bounded by the same budget).
        await self._refresh_until_match(None, effective_timeout)

        # Alias fallback: the model may use a generic synonym for a tool that
        # is registered under a more specific name.
        by_normalized = {
            normalize_tool_name(item.name): item for item in self.registry.all()
        }
        for candidate in _TOOL_NAME_ALIASES.get(normalized_tool, ()):
            match = by_normalized.get(candidate)
            if match is not None:
                resolved = _from_entry(match)
                if resolved is not None:
                    logger.debug(f"resolved tool alias '{tool}' -> '{match.name}'")
                    return resolved

        # Server-name fallback: if exactly one tool belongs to the named
        # server, the caller probably meant that tool.
        for server_name, client in clients.items():
            if self._normalize_tool_name(server_name) == normalized_tool:
                server_tools = self.registry.for_server(server_name)
                if len(server_tools) == 1:
                    return (client, server_tools[0].name)
                break

        return None

    async def suggest_tools(self, tool: str, timeout: float | None = None) -> list[str]:
        """Return registered tool names close to a misnamed call.

        Reads the warm registry; no per-client discovery RPCs.
        """
        normalized_tool = self._normalize_tool_name(tool)
        clients = dict(self.get_clients())
        if not clients:
            return []

        self._sync_registry_servers()
        effective_timeout = (
            float(timeout) if timeout is not None else DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
        )
        await self._refresh_until_match(None, effective_timeout)

        # Server-name match: the model likely meant a tool on this server.
        for server_name in clients:
            if self._normalize_tool_name(server_name) == normalized_tool:
                return [
                    entry.exposed or entry.name
                    for entry in self.registry.for_server(server_name)
                ]

        all_tool_names = [entry.exposed or entry.name for entry in self.registry.all()]
        if not all_tool_names:
            return []

        matches = difflib.get_close_matches(
            normalized_tool,
            [self._normalize_tool_name(name) for name in all_tool_names],
            n=5,
            cutoff=0.4,
        )
        normalized_to_original = {
            self._normalize_tool_name(name): name for name in all_tool_names
        }
        return [normalized_to_original[match] for match in matches]

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
