import asyncio
import json

import httpx

from mcp_bridge.openai_clients import streamChatCompletion as stream_module
from mcp_bridge.openai_clients.chatCompletion import _message_role, _message_tool_call_id

# `CreateChatCompletionRequest` is intentionally read off the already-imported
# `stream_module` rather than via a fresh top-level `from lmos_openai_types
# import ...` here. tests/test_diagnostic_snippet.py replaces
# `sys.modules["lmos_openai_types"]` with a fake stub module and never
# restores it, which would otherwise poison a fresh import in any test file
# collected after it (alphabetically, this one). `stream_module` was already
# imported with the real types by test_config_hardening.py (which sorts
# before the polluting file), so its bound name is unaffected.
CreateChatCompletionRequest = stream_module.CreateChatCompletionRequest


class _FakeSSEEvent:
    def __init__(self, data: str, event: str = "message", id: str = "", retry=None):
        self.data = data
        self.event = event
        self.id = id
        self.retry = retry


class _FakeResponse:
    headers = {"Content-Type": "text/event-stream"}
    url = "http://fake/v1/chat/completions"
    status_code = 200
    encoding = "utf-8"


class _FakeEventSource:
    def __init__(self, chunks: list[str], raises: Exception | None = None):
        self._chunks = chunks
        self._raises = raises
        self.response = _FakeResponse()

    async def aiter_sse(self):
        for chunk in self._chunks:
            yield _FakeSSEEvent(chunk)
        if self._raises is not None:
            raise self._raises


class _FakeSSEConnectContextManager:
    def __init__(self, chunks: list[str], raises: Exception | None = None):
        self._chunks = chunks
        self._raises = raises

    async def __aenter__(self):
        return _FakeEventSource(self._chunks, raises=self._raises)

    async def __aexit__(self, *exc_info):
        return False


class _FakeClientContextManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


class _FakeErrorResponse:
    """An upstream response that isn't an SSE stream at all (e.g. a 502 with a
    plain JSON error body), as opposed to a stream that starts fine and then
    breaks mid-way."""

    def __init__(self, body: bytes, status_code: int = 502):
        self.headers = {"Content-Type": "application/json"}
        self.url = "http://fake/v1/chat/completions"
        self.status_code = status_code
        self.encoding = "utf-8"
        self._body = body

    async def aread(self):
        return self._body


class _FakeErrorEventSource:
    def __init__(self, body: bytes, status_code: int = 502):
        self.response = _FakeErrorResponse(body, status_code=status_code)

    async def aiter_sse(self):
        return
        yield  # pragma: no cover - makes this an async generator; never reached


class _FakeErrorConnectContextManager:
    def __init__(self, body: bytes, status_code: int = 502):
        self._body = body
        self._status_code = status_code

    async def __aenter__(self):
        return _FakeErrorEventSource(self._body, status_code=self._status_code)

    async def __aexit__(self, *exc_info):
        return False


def _patch_stream_with_upstream_error(monkeypatch, body: bytes, status_code: int = 502):
    def fake_aconnect_sse(client, method, url, content=None):
        return _FakeErrorConnectContextManager(body, status_code=status_code)

    def fake_get_client(*args, **kwargs):
        return _FakeClientContextManager()

    monkeypatch.setattr(stream_module, "aconnect_sse", fake_aconnect_sse)
    monkeypatch.setattr(stream_module, "get_client", fake_get_client)
    monkeypatch.setattr(stream_module, "chat_completion_add_tools", _fake_add_tools)


async def _fake_add_tools(request):
    # Bypass real MCP client discovery entirely -- these tests exercise only
    # the SSE-chunk parsing logic, not tool dispatch, and the real
    # ClientManager would try to construct/connect actual subprocess or
    # network-backed MCP clients from config.json outside its intended
    # container environment.
    request.tools = []
    return request


def _patch_stream(monkeypatch, chunks: list[str], raises: Exception | None = None):
    def fake_aconnect_sse(client, method, url, content=None):
        return _FakeSSEConnectContextManager(chunks, raises=raises)

    def fake_get_client(*args, **kwargs):
        return _FakeClientContextManager()

    monkeypatch.setattr(stream_module, "aconnect_sse", fake_aconnect_sse)
    monkeypatch.setattr(stream_module, "get_client", fake_get_client)
    monkeypatch.setattr(stream_module, "chat_completion_add_tools", _fake_add_tools)


def _patch_stream_multi_round(monkeypatch, chunk_batches: list[list[str]]):
    # One batch of SSE chunks per upstream connection the outer `while not
    # fully_done` loop makes (e.g. a tool-calling round followed by the
    # model's next-turn response).
    batches = list(chunk_batches)

    def fake_aconnect_sse(client, method, url, content=None):
        chunks = batches.pop(0) if batches else ["[DONE]"]
        return _FakeSSEConnectContextManager(chunks)

    def fake_get_client(*args, **kwargs):
        return _FakeClientContextManager()

    monkeypatch.setattr(stream_module, "aconnect_sse", fake_aconnect_sse)
    monkeypatch.setattr(stream_module, "get_client", fake_get_client)
    monkeypatch.setattr(stream_module, "chat_completion_add_tools", _fake_add_tools)


def _make_request() -> CreateChatCompletionRequest:
    return CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    )


def _run_stream(request):
    async def collect():
        results = []
        async for item in stream_module.chat_completions(request, None):
            results.append(item)
        return results

    return asyncio.run(collect())


def test_stream_lowercases_uppercase_finish_reason(monkeypatch):
    # OpenRouter (and others) sometimes send an uppercase finish_reason. The
    # strict lowercase-only enum on CreateChatCompletionStreamResponse rejects
    # it outright unless it's normalized before validation.
    chunk = json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "STOP"}],
        }
    )
    _patch_stream(monkeypatch, [chunk, "[DONE]"])

    # Before the fix, this raised a pydantic ValidationError (uppercase
    # "STOP" rejected by the strict lowercase-only enum) instead of forwarding
    # the chunk. Forwarding it at all -- content intact -- proves the
    # normalization happened before validation.
    results = _run_stream(_make_request())
    forwarded = json.loads(results[0])
    assert forwarded["choices"][0]["delta"]["content"] == "hi"


def test_stream_handles_already_lowercase_finish_reason(monkeypatch):
    chunk = json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}],
        }
    )
    _patch_stream(monkeypatch, [chunk, "[DONE]"])

    results = _run_stream(_make_request())
    forwarded = json.loads(results[0])
    assert forwarded["choices"][0]["delta"]["content"] == "hi"


def test_stream_yields_error_chunk_when_stream_ends_without_a_finish_reason(monkeypatch):
    # A stream that ends ("[DONE]") without any chunk ever carrying a
    # finish_reason is itself an upstream protocol violation, independent of
    # the finish_reason-casing fix -- this is the correct behavior, not a
    # regression. It also exercises the null/missing finish_reason path
    # through the new normalization code (`.lower()` on None raises
    # AttributeError, which must be swallowed the same way a missing key is).
    #
    # This must NOT raise HTTPException: by the time this is detected, the SSE
    # response has already been committed to the client (200 OK plus
    # text/event-stream headers), so raising can't change the status code the
    # client sees -- it would only abort the connection with a noisy
    # unhandled-exception stack trace. It should instead yield a parseable
    # error chunk and end the stream cleanly.
    chunk = json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
        }
    )
    _patch_stream(monkeypatch, [chunk, "[DONE]"])

    results = _run_stream(_make_request())
    # results[0] is the "partial" content chunk forwarded before the stream
    # ended; the error chunk this fix adds comes right after it.
    error_chunk = json.loads(results[1])
    assert error_chunk["error"]["code"] == 502


def test_stream_ends_cleanly_when_client_disconnects_mid_stream(monkeypatch):
    # Reproduces a real production trace: the downstream client disconnects
    # (or the upstream connection drops) while still reading the SSE stream,
    # surfacing as httpx.ReadError from inside `event_source.aiter_sse()`.
    # Before the fix this propagated all the way out of our generator into
    # sse_starlette's task group, logged as an "Exception in ASGI application"
    # stack trace. It should instead end the generator quietly.
    chunk = json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test",
            "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
        }
    )
    _patch_stream(monkeypatch, [chunk], raises=httpx.ReadError("simulated connection drop"))

    # Must not raise -- the generator should end cleanly instead.
    results = _run_stream(_make_request())
    forwarded = json.loads(results[0])
    assert forwarded["choices"][0]["delta"]["content"] == "partial"


class _FakeConnectFailsContextManager:
    """Simulates aconnect_sse's __aenter__ itself failing -- i.e. the upstream
    connection is dropped/reset before any SSE response headers arrive, as
    opposed to a stream that starts fine and breaks mid-iteration."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc_info):
        return False


def _patch_stream_connect_fails(monkeypatch, exc: Exception):
    def fake_aconnect_sse(client, method, url, content=None):
        return _FakeConnectFailsContextManager(exc)

    def fake_get_client(*args, **kwargs):
        return _FakeClientContextManager()

    monkeypatch.setattr(stream_module, "aconnect_sse", fake_aconnect_sse)
    monkeypatch.setattr(stream_module, "get_client", fake_get_client)
    monkeypatch.setattr(stream_module, "chat_completion_add_tools", _fake_add_tools)


def test_stream_yields_error_chunk_when_upstream_connection_fails_to_establish(monkeypatch):
    # Reproduces a real production trace: httpx.ReadError raised from inside
    # `aconnect_sse(...).__aenter__()` -- the connection to the upstream
    # inference server itself failed, before any SSE event ever arrived (a
    # distinct call site from the mid-stream disconnect covered by
    # test_stream_ends_cleanly_when_client_disconnects_mid_stream, which is
    # already wrapped in its own try/except that does not cover __aenter__).
    # Before the fix this propagated out of our generator into sse_starlette's
    # task group, logged as an unhandled "Exception in ASGI application" /
    # ExceptionGroup stack trace. It should instead yield a clean error chunk.
    _patch_stream_connect_fails(monkeypatch, httpx.ReadError("simulated connection drop"))

    results = _run_stream(_make_request())

    assert len(results) == 2
    forwarded_error = json.loads(results[0])
    assert "upstream connection failed" in forwarded_error["error"]["message"]
    assert isinstance(results[1], stream_module.ServerSentEvent)
    assert results[1].data == "[DONE]"


class _FakeToolResult:
    def __init__(self, text: str):
        self.isError = False
        self.content = [type("ToolTextContent", (), {"type": "text", "text": text})()]

    def model_dump(self, **kwargs):
        return {"isError": False, "content": [{"type": "text", "text": self.content[0].text}]}


def test_stream_appends_placeholder_reply_for_undispatched_tool_call(monkeypatch):
    # Reproduces a real production trace: the model streamed 5 parallel tool
    # calls, but one of them never got a `function.name` delta (an upstream
    # streaming-format quirk), leaving it merged with an empty name.
    # `call_tool` refuses to dispatch a nameless call and returns None for it
    # -- but the assistant message already lists that call's id in its
    # `tool_calls`. Before the fix, the loop just `continue`d past a None
    # result, leaving that id with no matching `tool` reply: the next
    # request then has an assistant `tool_calls` message with an orphaned
    # id, which strict providers reject outright with "tool call result
    # does not follow tool call" -- the same failure class this project hit
    # and fixed for the non-streaming loop, just via a different trigger,
    # in the streaming loop's own separate implementation.
    round1_chunks = [
        json.dumps(
            {
                "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [
                        {"index": 0, "id": "call_good", "type": "function", "function": {"name": "search", "arguments": ""}}
                    ]},
                    "finish_reason": None,
                }],
            }
        ),
        json.dumps(
            {
                "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [
                        # No "name" ever sent for index 1 -- the upstream bug.
                        {"index": 1, "id": "call_orphan", "type": "function", "function": {"arguments": "{}"}}
                    ]},
                    "finish_reason": None,
                }],
            }
        ),
        json.dumps(
            {
                "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }
        ),
        "[DONE]",
    ]
    round2_chunks = [
        json.dumps(
            {
                "id": "c2", "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
            }
        ),
        "[DONE]",
    ]
    _patch_stream_multi_round(monkeypatch, [round1_chunks, round2_chunks])

    async def fake_call_tools(tool_calls, **kwargs):
        # Mirrors the real `call_tool`: a nameless call dispatches to None.
        results = []
        for name, _arguments in tool_calls:
            results.append(_FakeToolResult("ok") if name else None)
        return results

    monkeypatch.setattr(stream_module, "call_tools", fake_call_tools)

    request = _make_request()
    _run_stream(request)  # `request.messages` accumulates in place as the generator runs

    tool_messages = [m for m in request.messages if _message_role(m) == "tool"]
    replied_ids = {_message_tool_call_id(m) for m in tool_messages}
    assert "call_good" in replied_ids
    assert "call_orphan" in replied_ids, "the undispatched (nameless) tool call must still get a reply"


def test_stream_yields_error_chunk_when_upstream_returns_non_stream_error(monkeypatch):
    # Reproduces a real production trace: the upstream provider (Nvidia, via
    # OpenRouter) returned a 502 with a plain JSON error body instead of an
    # SSE stream. Before the fix, this was detected and logged correctly but
    # then raised as HTTPException(500) from inside the generator -- by that
    # point sse_starlette had already committed the 200/text-event-stream
    # response to the client, so the raise couldn't change what the client
    # saw; it just aborted the connection with a noisy unhandled "Exception
    # in ASGI application" stack trace. It should instead forward the
    # upstream's own error body to the client as a clean chunk.
    upstream_error_body = json.dumps(
        {
            "error": {
                "message": "Provider returned error",
                "code": 502,
                "metadata": {"raw": "error code: 502\n", "provider_name": "Nvidia", "is_byok": False},
            },
            "user_id": "user_abc123",
        }
    ).encode("utf-8")
    _patch_stream_with_upstream_error(monkeypatch, upstream_error_body, status_code=502)

    # Must not raise -- the generator should forward the error and end cleanly.
    results = _run_stream(_make_request())
    forwarded_error = json.loads(results[0])
    assert forwarded_error["error"]["message"] == "Provider returned error"
    assert forwarded_error["error"]["metadata"]["provider_name"] == "Nvidia"


def _tool_call_round(round_id: str, *, index: int = 0) -> list[str]:
    """One upstream round that streams a single tool call, then [DONE]."""
    return [
        json.dumps(
            {
                "id": round_id, "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [
                        {"index": index, "id": f"call_{index}", "type": "function",
                         "function": {"name": "web_search", "arguments": '{"query": "x"}'}}
                    ]},
                    "finish_reason": None,
                }],
            }
        ),
        json.dumps(
            {
                "id": round_id, "object": "chat.completion.chunk", "created": 0, "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }
        ),
        "[DONE]",
    ]


def test_stream_stops_on_repeated_tool_call(monkeypatch):
    """The streamed tool loop must stop when the same tool call is repeated
    (mirroring the non-streaming `_detect_repeated_tool_calls` guard).

    Regression: a model that kept calling a *non-existent* tool (observed:
    `web_search` 4x in one batch, then again next turn) could previously spin
    because the streaming loop had no repeat guard at all.
    """
    # Four rounds of the identical tool call; the guard trips on the 3rd.
    rounds = [_tool_call_round(f"r{i}") for i in range(4)]
    rounds.append(
        [
            json.dumps(
                {
                    "id": "final", "object": "chat.completion.chunk", "created": 0, "model": "test",
                    "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
                }
            ),
            "[DONE]",
        ]
    )
    _patch_stream_multi_round(monkeypatch, rounds)

    async def fake_call_tools(tool_calls, **kwargs):
        # The tool does not exist -> every dispatch fails, like the real
        # `web_search` hallucination.
        return [_FakeToolResult("no MCP client found for tool 'web_search'") for _ in tool_calls]

    monkeypatch.setattr(stream_module, "call_tools", fake_call_tools)

    results = _run_stream(_make_request())

    # The loop stopped via the repeated-call guard and emitted a final
    # assistant message + a clean [DONE] sentinel, rather than looping forever.
    joined = " ".join(str(item) for item in results)
    assert "repeated without new information" in joined
    assert any(getattr(item, "data", None) == "[DONE]" for item in results)

