import asyncio
import contextlib
import json
import os
from typing import Any

import httpx
from loguru import logger

try:
    from mcp import McpError
    from mcp.client.sse import sse_client
    import mcp.types as types
except ImportError:  # pragma: no cover - allows the package to import in minimal environments
    class McpError(RuntimeError):
        pass

    async def sse_client(*args: Any, **kwargs: Any):
        raise RuntimeError("mcp SDK is not installed")

    types = Any

from mcp_bridge import __version__
from mcp_bridge.config import config
from mcp_bridge.config.final import SSEMCPServer
from .AbstractClient import GenericMcpClient

try:  # Python 3.11+
    _BASE_EXCEPTION_GROUP: type[BaseException] | None = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover - older runtimes
    _BASE_EXCEPTION_GROUP = None

# How long a `ping` (a liveness probe, NOT a data request) is allowed to wait
# for its reply before the session is considered dead.
#
# This is deliberately decoupled from `requestTimeout`. `requestTimeout` is a
# *data* timeout -- a search server may legitimately take 120s to answer a
# `tools/call`. Reusing it as the ping timeout meant a ping queued behind a
# long-running tool call waited the FULL request budget and then declared the
# session dead, tearing down a perfectly healthy server mid-workflow. A ping
# only proves liveness, so it needs a much shorter leash.
DEFAULT_SSE_PING_TIMEOUT_SECONDS = 20.0

# Sentinel distinguishing "caller did not override the read timeout" from
# "caller explicitly wants no timeout" (None).
_UNSET_TIMEOUT = object()


def get_sse_ping_timeout_seconds() -> float:
    """Resolve the liveness-ping timeout, overridable via environment.

    Kept short by default because it measures liveness only. Operators whose
    remote server is genuinely slow to answer even a `ping` can raise it.
    """
    raw_value = os.getenv("MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_SSE_PING_TIMEOUT_SECONDS

    try:
        value = float(raw_value)
    except ValueError:
        logger.warning(
            f"invalid MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS value: {raw_value}; "
            f"using default {DEFAULT_SSE_PING_TIMEOUT_SECONDS}"
        )
        return DEFAULT_SSE_PING_TIMEOUT_SECONDS

    if value <= 0:
        logger.warning(
            f"MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS={value} must be > 0; "
            f"using default {DEFAULT_SSE_PING_TIMEOUT_SECONDS}"
        )
        return DEFAULT_SSE_PING_TIMEOUT_SECONDS
    return value


def _describe_exception(exc: BaseException, *, depth: int = 0) -> str:
    """Render an exception as a non-empty, greppable description.

    ``f"{exc}"`` is empty for several exceptions that routinely surface on SSE
    teardown (``ExceptionGroup`` with empty sub-exception messages, bare
    ``ClosedResourceError``/``EndOfStream``). That produced useless
    ``ping failed for google-search: `` log lines with no error text. This
    always includes the type name and recurses into ``ExceptionGroup`` members.
    """
    try:
        if _BASE_EXCEPTION_GROUP is not None and isinstance(exc, _BASE_EXCEPTION_GROUP):
            members = "; ".join(
                _describe_exception(sub, depth=depth + 1) for sub in exc.exceptions
            )
            return f"{type(exc).__name__}({members})"
        text = str(exc).strip()
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    except Exception:
        return type(exc).__name__


def _client_capabilities(*, supports_server_requests: bool) -> dict[str, Any]:
    """Capabilities this client can actually honour.

    ``roots`` is advertised because ``roots/list`` is answered with an empty
    list; ``listChanged`` is not, because the bridge never emits that
    notification. ``sampling`` is advertised only when at least one model is
    configured. A transport with no persistent read stream cannot answer
    server-initiated requests at all, so it advertises nothing.
    """
    if not supports_server_requests:
        return {}
    capabilities: dict[str, Any] = {"roots": {}}
    if getattr(getattr(config, "sampling", None), "models", None):
        capabilities["sampling"] = {}
    return capabilities


def _server_headers(server_config: Any) -> dict[str, str]:
    """Build request headers from a server's ``headers`` and ``auth`` config.

    ``headers`` is the general mechanism and wins. A bearer ``auth`` entry is
    mapped to ``Authorization`` only when headers do not already set one; the
    previous code never read ``auth`` at all, so remote servers that required a
    token could only be configured through URL query strings (which leak into
    logs).
    """
    headers = {
        str(key): str(value)
        for key, value in (getattr(server_config, "headers", None) or {}).items()
    }
    auth = getattr(server_config, "auth", None) or {}
    if isinstance(auth, dict):
        auth_type = str(auth.get("type", "bearer")).lower()
        token = auth.get("token") or auth.get("api_key")
        if token and auth_type not in {"none", "basic"}:
            headers.setdefault("Authorization", f"Bearer {token}")
    return headers


class HttpMcpSession:
    def __init__(
        self,
        url: str,
        read_timeout_seconds: float | None = None,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._read_timeout_seconds = read_timeout_seconds
        self._configured_headers = dict(headers or {})
        # Test seam: an in-process transport avoids a real network round trip.
        self._transport = transport
        self._request_id = 1
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        # One client for the whole session. The previous implementation created
        # and closed a client per JSON-RPC message, losing keep-alive and making
        # a stateful Streamable HTTP session impossible.
        self._client: httpx.AsyncClient | None = None

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self._configured_headers,
        }
        # Session headers are protocol state, not operator defaults: apply them
        # last so a configured header cannot break the session by accident.
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = (
                None
                if self._read_timeout_seconds is None
                else float(self._read_timeout_seconds)
            )
            self._client = httpx.AsyncClient(timeout=timeout, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        """Delete the server-side session (if any) and close the client."""
        client = self._client
        self._client = None
        if client is None:
            return
        if self._session_id:
            try:
                response = await client.delete(
                    self._url, headers=self._request_headers()
                )
                response.raise_for_status()
            except Exception as exc:
                logger.debug(
                    f"failed to delete MCP session: {_describe_exception(exc)}"
                )
        try:
            await client.aclose()
        except Exception as exc:
            logger.debug(
                f"failed to close MCP HTTP client: {_describe_exception(exc)}"
            )
        self._session_id = None
        self._protocol_version = None

    async def initialize(self) -> Any:
        response = await self._send_request(
            "initialize",
            {
                "protocolVersion": getattr(types, "LATEST_PROTOCOL_VERSION", "2024-11-05"),
                "capabilities": _client_capabilities(supports_server_requests=False),
                "clientInfo": {"name": "MCP-Bridge", "version": __version__},
            },
            result_type=types.InitializeResult,
            allow_reinit=False,
        )
        negotiated = getattr(response, "protocolVersion", None)
        if isinstance(negotiated, str) and negotiated:
            self._protocol_version = negotiated
        await self._send_notification("notifications/initialized", None)
        return response

    async def send_ping(self) -> Any:
        return await self._send_request("ping", None, result_type=types.EmptyResult)

    async def list_tools(self) -> Any:
        return await self._send_request("tools/list", None, result_type=types.ListToolsResult)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            result_type=types.CallToolResult,
        )

    async def _send_request(
        self, method: str, params: Any, result_type: Any, *, allow_reinit: bool = True
    ) -> Any:
        request_id = self._request_id
        self._request_id += 1

        normalized_params = {} if params is None else params
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": normalized_params,
        }

        response = await self._post_jsonrpc(payload, allow_reinit=allow_reinit)
        if not isinstance(response, dict) or "result" not in response:
            # The SDK's McpError expects an ErrorData object (it reads
            # `error.message`), not a plain string. Passing a string here
            # previously raised `'str' object has no attribute 'message'`,
            # masking the real "invalid response payload" error.
            raise McpError(
                types.ErrorData(
                    code=-32603,
                    message="Invalid response payload",
                )
            )

        return result_type.model_validate(response["result"])

    async def _send_notification(self, method: str, params: Any) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {} if params is None else params,
        }
        await self._post_jsonrpc(payload)

    async def _post_jsonrpc(
        self, payload: dict[str, Any], *, allow_reinit: bool = True
    ) -> dict[str, Any]:
        client = self._get_client()
        async with client.stream(
            "POST", self._url, headers=self._request_headers(), json=payload
        ) as response:
            # Always trust the latest id: servers may rotate it.
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                self._session_id = session_id

            if response.status_code == 404 and self._session_id and allow_reinit:
                # The session expired server-side. Drain the body so the
                # connection can be released, initialize once, then retry. The
                # flag prevents a loop if initialize itself is rejected.
                await response.aread()
                logger.info(f"MCP session expired for {self._url}; re-initializing")
                await self.initialize()
                return await self._post_jsonrpc(payload, allow_reinit=False)

            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            content_length = response.headers.get("content-length", "")
            if response.status_code in {202, 204} or content_length == "0":
                return {}

            if "application/json" in content_type:
                return json.loads((await response.aread()).decode("utf-8"))

            return await self._parse_sse_response(response)

    async def _parse_sse_response(self, response: httpx.Response) -> dict[str, Any]:
        event_name: str | None = None
        data_lines: list[str] = []

        async for line in response.aiter_lines():
            if not line:
                if event_name == "message" and data_lines:
                    return json.loads("\n".join(data_lines))
                event_name = None
                data_lines = []
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())

        if event_name == "message" and data_lines:
            return json.loads("\n".join(data_lines))

        raise McpError(
            types.ErrorData(
                code=-32603,
                message="No SSE message payload received",
            )
        )


class SseMcpSession:
    def __init__(
        self,
        read_stream: Any,
        write_stream: Any,
        read_timeout_seconds: float | None = None,
        ping_timeout_seconds: float | None = _UNSET_TIMEOUT,  # type: ignore[assignment]
    ) -> None:
        self._read_stream = read_stream
        self._write_stream = write_stream
        self._read_timeout_seconds = read_timeout_seconds
        # A ping is a liveness probe, so by default it uses a short, dedicated
        # timeout instead of the (much larger) data `read_timeout_seconds`.
        # Callers that explicitly pass a value -- including None -- keep it.
        self._ping_timeout_seconds = (
            get_sse_ping_timeout_seconds()
            if ping_timeout_seconds is _UNSET_TIMEOUT
            else ping_timeout_seconds
        )
        self._pending_responses: dict[int, asyncio.Future[Any]] = {}
        self._request_id = 1
        self._message_task: asyncio.Task[None] | None = None
        # Server-initiated sampling runs off the read loop; keep a strong
        # reference and cancel it on teardown.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def __aenter__(self) -> "SseMcpSession":
        self._message_task = asyncio.create_task(self._message_loop())
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        # Fail every in-flight request so no caller is left awaiting a reply
        # that can never arrive now the stream is closing. Without this, a
        # teardown triggered by one request (e.g. a ping timeout) could leave
        # unrelated in-flight calls hanging until their own timeout.
        for future in list(self._pending_responses.values()):
            if not future.done():
                future.cancel()
        self._pending_responses.clear()

        for background_task in list(self._background_tasks):
            background_task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        task = self._message_task
        self._message_task = None
        if task is None:
            return

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await task
            except Exception as exc:
                # CRITICAL: this runs while an existing exception (e.g. the
                # TimeoutError from a failed ping) is already propagating out
                # of the `async with` body. Raising here would REPLACE that
                # exception, which previously surfaced as the misleading
                #   ExceptionGroup('unhandled errors in a TaskGroup',
                #                   [InvalidStateError('invalid state')])
                # and completely hid the real cause. Log it and let the
                # original exception continue unwinding instead.
                logger.debug(
                    f"SSE message loop exited with {_describe_exception(exc)} "
                    "during session teardown"
                )

    async def _message_loop(self) -> None:
        while True:
            message = await self._read_stream.receive()
            if isinstance(message, Exception):
                logger.error(f"SSE stream error: {message}")
                continue

            root = getattr(message, "root", None)
            if root is None:
                continue

            if isinstance(root, types.JSONRPCResponse):
                self._resolve_future(
                    getattr(root, "id", None),
                    result=root,
                )
            elif isinstance(root, types.JSONRPCError):
                self._resolve_future(
                    getattr(root, "id", None),
                    exception=McpError(root.error),
                )
            elif isinstance(root, types.JSONRPCNotification):
                logger.debug(f"received notification from SSE server: {root}")
            elif isinstance(root, types.JSONRPCRequest):
                # A server may probe liveness (ping), ask for roots, or request
                # sampling. Logging and dropping these left the server waiting
                # forever, which could stall the very tool call we were serving.
                await self._handle_server_request(root)

    async def _handle_server_request(self, request: Any) -> None:
        """Answer a server-initiated JSON-RPC request.

        ``ping`` and ``roots/list`` are immediate and answered inline.
        Sampling calls the upstream model, so it runs as a tracked background
        task: blocking the read loop would delay tool-call responses, including
        the one whose server is waiting on this request.
        """
        method = getattr(request, "method", "")
        request_id = getattr(request, "id", None)

        if method == "ping":
            await self._send_result(request_id, {})
            return
        if method == "roots/list":
            # The bridge exposes no roots; an empty list is the honest answer.
            await self._send_result(request_id, {"roots": []})
            return
        if method == "sampling/createMessage":
            task = asyncio.create_task(self._handle_sampling_request(request))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return

        logger.debug(f"answering unsupported server request from SSE server: {method}")
        await self._send_error(request_id, -32601, f"Method not found: {method}")

    async def _handle_sampling_request(self, request: Any) -> None:
        request_id = getattr(request, "id", None)
        try:
            params = types.CreateMessageRequestParams.model_validate(
                getattr(request, "params", None) or {}
            )
            # Lazy import: sampling pulls in the upstream HTTP client, and this
            # module is imported while MCP clients are being constructed.
            from mcp_bridge.sampling import sampler

            result = await sampler.handle_sampling_message(params)
            await self._send_result(
                request_id, result.model_dump(by_alias=True, exclude_none=True)
            )
        except Exception as exc:
            logger.warning(
                f"sampling request from SSE server failed: {_describe_exception(exc)}"
            )
            await self._send_error(request_id, -32603, "Sampling request failed")

    async def _send_result(self, request_id: Any, result: Any) -> None:
        await self._send_message(
            types.JSONRPCMessage(
                types.JSONRPCResponse(jsonrpc="2.0", id=request_id, result=result)
            ),
            request_id,
        )

    async def _send_error(self, request_id: Any, code: int, message: str) -> None:
        await self._send_message(
            types.JSONRPCMessage(
                types.JSONRPCError(
                    jsonrpc="2.0",
                    id=request_id,
                    error=types.ErrorData(code=code, message=message),
                )
            ),
            request_id,
        )

    async def _send_message(self, message: Any, request_id: Any) -> None:
        try:
            await self._write_stream.send(message)
        except Exception as exc:
            # The peer may be gone (normal during teardown); do not kill the
            # read loop over a reply that can no longer be delivered.
            logger.debug(
                f"could not answer server request {request_id}: {_describe_exception(exc)}"
            )

    def _resolve_future(
        self,
        request_id: Any,
        *,
        result: Any = None,
        exception: BaseException | None = None,
    ) -> None:
        """Settle the future for ``request_id`` if it is still pending.

        ``asyncio.wait_for`` CANCELS the awaited future the instant it times
        out, while the `finally` that drops the map entry only runs on the next
        event-loop tick. A reply arriving in that window previously hit an
        unconditional ``set_result()`` on an already-cancelled future, raising
        ``asyncio.InvalidStateError('invalid state')`` INSIDE the message-loop
        task. That killed the loop and its exception later masked the real
        timeout during `sse_client`'s TaskGroup teardown. Treating an
        already-settled future as a normal "too late, ignore it" case removes
        the race entirely.
        """
        future = self._pending_responses.pop(request_id, None)
        if future is None or future.done():
            return
        if exception is not None:
            future.set_exception(exception)
        else:
            future.set_result(result)

    async def initialize(self) -> Any:
        response = await self._send_request(
            "initialize",
            {
                "protocolVersion": getattr(types, "LATEST_PROTOCOL_VERSION", "2024-11-05"),
                "capabilities": _client_capabilities(supports_server_requests=True),
                "clientInfo": {"name": "MCP-Bridge", "version": __version__},
            },
            result_type=types.InitializeResult,
        )
        await self._send_notification("notifications/initialized", None)
        return response

    async def send_ping(self) -> Any:
        return await self._send_request(
            "ping",
            None,
            result_type=types.EmptyResult,
            timeout_seconds=self._ping_timeout_seconds,
        )

    async def list_tools(self) -> Any:
        return await self._send_request("tools/list", None, result_type=types.ListToolsResult)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            result_type=types.CallToolResult,
        )

    async def _send_request(
        self,
        method: str,
        params: Any,
        result_type: Any,
        timeout_seconds: float | None = _UNSET_TIMEOUT,  # type: ignore[assignment]
    ) -> Any:
        request_id = self._request_id
        self._request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_responses[request_id] = future

        normalized_params = {} if params is None else params
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": normalized_params,
        }
        await self._write_stream.send(types.JSONRPCMessage(types.JSONRPCRequest(**payload)))

        try:
            # An explicit per-call timeout wins; otherwise fall back to the
            # session-wide data timeout (None means "wait indefinitely").
            effective_timeout = (
                self._read_timeout_seconds
                if timeout_seconds is _UNSET_TIMEOUT
                else timeout_seconds
            )
            timeout = None if effective_timeout is None else float(effective_timeout)
            if timeout is None:
                response = await future
            else:
                response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_responses.pop(request_id, None)

        if not hasattr(response, "result"):
            raise McpError(
                types.ErrorData(
                    code=-32603,
                    message="Invalid response payload",
                )
            )
        return result_type.model_validate(response.result)

    async def _send_notification(self, method: str, params: Any) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {} if params is None else params,
        }
        await self._write_stream.send(types.JSONRPCMessage(types.JSONRPCNotification(**payload)))


class SseClient(GenericMcpClient):
    config: SSEMCPServer

    def __init__(self, name: str, config: SSEMCPServer) -> None:
        super().__init__(name=name)

        self.config = config

    async def _maintain_session(self) -> None:
        async with sse_client(self.config.url) as client:
            async with SseMcpSession(
                *client,
                read_timeout_seconds=self.config.requestTimeout / 1000.0 if self.config.requestTimeout else None,
            ) as session:
                await session.initialize()
                logger.debug(f"finished initialise session for {self.name}")
                self.session = session

                try:
                    while True:
                        await asyncio.sleep(10)

                        # Never ping while a tool call is in flight: an
                        # in-flight call is itself proof the session is alive,
                        # and a ping queued behind a long search would
                        # otherwise "time out" and tear down a healthy server
                        # mid-workflow. This was the actual cause of the
                        # spurious `ping failed for google-search` resets.
                        if not self.should_send_ping():
                            if config.logging.log_server_pings:
                                logger.debug(
                                    f"skipping ping for {self.name}: "
                                    f"{self._in_flight_calls} tool call(s) in flight"
                                )
                            continue

                        if config.logging.log_server_pings:
                            logger.debug(f"pinging session for {self.name}")

                        try:
                            await session.send_ping()
                        except (TimeoutError, asyncio.TimeoutError) as ping_exc:
                            # One lost ping is not proof of death -- a busy or
                            # briefly-bliping remote server can miss a single
                            # probe. Confirm with a second one before tearing
                            # the session down, so a transient blip does not
                            # reset every subsequent tool call.
                            logger.warning(
                                f"ping timed out for {self.name}: "
                                f"{_describe_exception(ping_exc)}; confirming before reset"
                            )
                            await asyncio.sleep(1)
                            if not self.should_send_ping():
                                continue
                            await session.send_ping()

                except Exception as exc:
                    # Include the exception *type* and a flattened view of any
                    # ExceptionGroup sub-exceptions. A bare `{exc}` renders
                    # empty for the common `ExceptionGroup` / closed-resource
                    # teardown case, which made "ping failed for X: " logs
                    # impossible to diagnose (no error text at all).
                    logger.error(
                        f"ping failed for {self.name}: {_describe_exception(exc)}"
                    )
                    self.session = None
                    raise

        logger.debug(f"exiting session for {self.name}")


class HttpClient(GenericMcpClient):
    config: SSEMCPServer

    def __init__(self, name: str, config: SSEMCPServer) -> None:
        super().__init__(name=name)
        self.config = config

    async def _maintain_session(self) -> None:
        session = HttpMcpSession(
            self.config.url,
            read_timeout_seconds=self.config.requestTimeout / 1000.0 if self.config.requestTimeout else None,
            headers=_server_headers(self.config),
        )
        try:
            await session.initialize()
            logger.debug(f"finished initialise session for {self.name}")
            self.session = session

            while True:
                await asyncio.sleep(10)

                # Same rationale as the SSE client: an in-flight tool call
                # already proves the server is reachable, and pinging during it
                # can queue behind the call and falsely report a dead session.
                if not self.should_send_ping():
                    if config.logging.log_server_pings:
                        logger.debug(
                            f"skipping ping for {self.name}: "
                            f"{self._in_flight_calls} tool call(s) in flight"
                        )
                    continue

                if config.logging.log_server_pings:
                    logger.debug(f"pinging session for {self.name}")

                try:
                    await session.send_ping()
                except (TimeoutError, asyncio.TimeoutError) as ping_exc:
                    logger.warning(
                        f"ping timed out for {self.name}: "
                        f"{_describe_exception(ping_exc)}; confirming before reset"
                    )
                    await asyncio.sleep(1)
                    if not self.should_send_ping():
                        continue
                    await session.send_ping()
        except Exception as exc:
            logger.error(f"ping failed for {self.name}: {_describe_exception(exc)}")
            self.session = None
            raise
        finally:
            # Always send DELETE and close the per-session client, including
            # when the maintainer is cancelled during shutdown.
            self.session = None
            await session.aclose()

        logger.debug(f"exiting session for {self.name}")
