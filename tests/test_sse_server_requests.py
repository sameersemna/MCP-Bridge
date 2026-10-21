"""Downstream clients must answer server->client requests (P0-07).

The SSE client advertised `sampling` and `roots` but only logged incoming
JSON-RPC requests, so a server waiting on `ping`, `sampling/createMessage` or
`roots/list` could stall a tool call. Capabilities must also match what the
transport can actually do.
"""

from __future__ import annotations

import asyncio

import anyio
import mcp.types as types

from mcp_bridge.config import config as app_config
from mcp_bridge.mcp_clients import SseClient as sse_module

SseMcpSession = sse_module.SseMcpSession

_TIMEOUT = 1.0


def _request(request_id: int, method: str, params: object = None) -> types.JSONRPCMessage:
    return types.JSONRPCMessage(
        types.JSONRPCRequest(
            jsonrpc="2.0", id=request_id, method=method, params=params
        )
    )


def _roundtrip(method: str, params: object = None) -> object:
    async def run():  # type: ignore[no-untyped-def]
        read_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_reader = anyio.create_memory_object_stream(0)
        async with SseMcpSession(read_stream, write_stream):
            await read_writer.send(_request(7, method, params))
            message = await asyncio.wait_for(write_reader.receive(), _TIMEOUT)
        return message.root

    return asyncio.run(run())


def test_ping_request_is_answered() -> None:
    response = _roundtrip("ping")
    assert isinstance(response, types.JSONRPCResponse)
    assert response.id == 7
    assert response.result == {}


def test_roots_list_returns_an_empty_list() -> None:
    response = _roundtrip("roots/list")
    assert isinstance(response, types.JSONRPCResponse)
    assert response.id == 7
    assert response.result == {"roots": []}


def test_unknown_server_request_gets_method_not_found() -> None:
    response = _roundtrip("made/up")
    assert isinstance(response, types.JSONRPCError)
    assert response.id == 7
    assert response.error.code == -32601


def test_sampling_request_is_answered_from_a_background_task(monkeypatch) -> None:
    async def fake_sampling(params):  # type: ignore[no-untyped-def]
        assert params.maxTokens == 16
        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text="sampled"),
            model="fake-model",
        )

    monkeypatch.setattr(
        "mcp_bridge.sampling.sampler.handle_sampling_message", fake_sampling
    )

    params = {
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "hello"}}
        ],
        "maxTokens": 16,
    }
    response = _roundtrip("sampling/createMessage", params)

    assert isinstance(response, types.JSONRPCResponse)
    assert response.id == 7
    assert response.result["model"] == "fake-model"
    assert response.result["content"]["text"] == "sampled"


def test_capabilities_only_advertise_what_is_implemented(monkeypatch) -> None:
    monkeypatch.setattr(app_config.sampling, "models", [], raising=False)
    assert sse_module._client_capabilities(supports_server_requests=True) == {"roots": {}}

    monkeypatch.setattr(app_config.sampling, "models", [object()], raising=False)
    assert sse_module._client_capabilities(supports_server_requests=True) == {
        "roots": {},
        "sampling": {},
    }

    # The request/response HTTP transport has no read loop, so it cannot answer
    # server-initiated requests and must not claim it can.
    assert sse_module._client_capabilities(supports_server_requests=False) == {}


def test_initialize_omits_sampling_when_no_models_are_configured(monkeypatch) -> None:
    monkeypatch.setattr(app_config.sampling, "models", [], raising=False)
    captured: dict[str, object] = {}

    async def run() -> None:
        read_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_reader = anyio.create_memory_object_stream(0)
        session = SseMcpSession(read_stream, write_stream)

        async def fake_send_request(method, params, result_type, **_kwargs):  # type: ignore[no-untyped-def]
            captured["method"] = method
            captured.update(params)
            return object()

        async def fake_notification(method, params):  # type: ignore[no-untyped-def]
            return None

        session._send_request = fake_send_request  # type: ignore[method-assign]
        session._send_notification = fake_notification  # type: ignore[method-assign]
        await session.initialize()

    asyncio.run(run())
    assert captured["method"] == "initialize"
    assert captured["capabilities"] == {"roots": {}}
