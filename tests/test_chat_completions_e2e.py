"""End-to-end tests for the `chat_completions` tool loop.

These drive the *public* `chat_completions` entry point (not just its private
helpers) with a fake upstream inference server and a fake tool executor, so the
full tool-calling state machine is exercised: tool-call dispatch, tool-result
injection, and final-answer synthesis.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException
from lmos_openai_types import CreateChatCompletionRequest

from mcp_bridge.mcp_clients.AbstractClient import CallToolResult, TextContent
from mcp_bridge.openai_clients import chatCompletion as chat_completion_module


class FakeResponse:
    def __init__(self, status_code: int, text: str, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeClient:
    """A fake httpx-like client that returns a scripted sequence of responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs):
        self.posts.append((url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _tool_calls_response(tool_name: str, arguments: str) -> str:
    return json.dumps(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": tool_name, "arguments": arguments},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
    )


def _stop_response(content: str) -> str:
    return json.dumps(
        {
            "id": "gen-2",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220},
        }
    )


def test_chat_completions_runs_tool_loop_and_returns_final_answer(monkeypatch):
    """A two-turn conversation: the model issues a tool call, the tool result is
    injected, and the model then produces a final answer."""
    fake_client = FakeClient(
        [
            FakeResponse(200, _tool_calls_response("search", '{"query": "hello"}')),
            FakeResponse(200, _stop_response("The final answer.")),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    async def fake_call_tools(tool_calls, **kwargs):
        return [
            CallToolResult(
                content=[TextContent(type="text", text=f"result for {name}")],
                isError=False,
            )
            for name, _ in tool_calls
        ]

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "call_tools", fake_call_tools)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "do a search"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    # Two upstream calls: the tool-call turn and the final-answer turn.
    assert len(fake_client.posts) == 2
    # The final answer is returned verbatim.
    assert response.choices[0].message.content == "The final answer."
    # The tool result was injected into the conversation before the final turn.
    final_request_json = fake_client.posts[1][1]["json"]
    assert any(
        m.get("role") == "tool" and "result for search" in str(m.get("content"))
        for m in final_request_json["messages"]
    )


def test_chat_completions_returns_immediate_answer_without_tools(monkeypatch):
    """A model that answers directly (no tool calls) returns on the first turn."""
    fake_client = FakeClient([FakeResponse(200, _stop_response("Direct answer."))])

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    assert len(fake_client.posts) == 1
    assert response.choices[0].message.content == "Direct answer."


def test_chat_completions_retries_transient_upstream_error(monkeypatch):
    """A transient upstream error (HTTP 200 with an error body) is retried and
    then succeeds, rather than failing the whole request."""
    fake_client = FakeClient(
        [
            FakeResponse(200, '{"error": {"message": "Service temporarily overloaded", "code": 502}}'),
            FakeResponse(200, _stop_response("Recovered answer.")),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    # Avoid the real 2s retry delay in tests.
    monkeypatch.setattr(chat_completion_module, "DEFAULT_UPSTREAM_RETRY_DELAY_SECONDS", 0.0)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    # The transient error was retried once, then the successful response was used.
    assert len(fake_client.posts) == 2
    assert response.choices[0].message.content == "Recovered answer."


def test_chat_completions_retries_transport_error_then_succeeds(monkeypatch):
    """Reproduces a real production trace: `client.post()` raised
    httpx.ReadError (the upstream connection dropped before any response
    arrived) instead of returning a response object at all. Before the fix
    this propagated straight out of the tool loop as an unhandled exception
    (an ugly "Exception in ASGI application" stack trace, and a 500 with no
    useful detail for the caller). It should instead be retried exactly like
    a transient 5xx status, using the same retry budget and backoff."""
    fake_client = FakeClient(
        [
            httpx.ReadError("connection reset"),
            FakeResponse(200, _stop_response("Recovered after transport error.")),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "DEFAULT_UPSTREAM_RETRY_DELAY_SECONDS", 0.0)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    assert len(fake_client.posts) == 2
    assert response.choices[0].message.content == "Recovered after transport error."


def test_chat_completions_fails_gracefully_after_repeated_transport_errors(monkeypatch):
    """When every attempt hits a transport error (connection never recovers),
    the request must still fail with a clean HTTPException carrying real
    diagnostic detail -- not an unhandled exception."""
    fake_client = FakeClient(
        [
            httpx.ReadError("connection reset"),
            httpx.ReadError("connection reset"),
            httpx.ReadError("connection reset"),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "DEFAULT_UPSTREAM_RETRY_DELAY_SECONDS", 0.0)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    try:
        asyncio.run(chat_completion_module.chat_completions(request, None))
        assert False, "expected an HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 502


def test_chat_completions_retries_429_and_honors_retry_after(monkeypatch):
    """A 429 rate-limit response is retryable, and the provider's `Retry-After`
    hint is honored (capped) so the provider can recover instead of us failing
    the request after a too-short fixed delay."""
    fake_client = FakeClient(
        [
            # 429 with a Retry-After header of 60s.
            FakeResponse(429, '{"error": {"message": "rate limited", "code": 429}}', headers={"Retry-After": "60"}),
            FakeResponse(200, _stop_response("Recovered successfully.")),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    # Cap the retry-after so the test doesn't actually sleep 60s.
    monkeypatch.setattr(chat_completion_module, "MAX_UPSTREAM_RETRY_AFTER_SECONDS", 0.0)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    # The 429 was retried once, then the successful response was used.
    assert len(fake_client.posts) == 2
    assert response.choices[0].message.content == "Recovered successfully."


def _multi_tool_calls_response(calls: list[tuple[str, str, str]], *, prompt_tokens: int = 100) -> str:
    """Like `_tool_calls_response` but with several tool calls in one turn.

    `calls` is a list of (call_id, tool_name, arguments_json) tuples.
    """
    return json.dumps(
        {
            "id": "gen-multi",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": tool_name, "arguments": arguments},
                            }
                            for call_id, tool_name, arguments in calls
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 10, "total_tokens": prompt_tokens + 10},
        }
    )


def test_pending_tool_calls_are_dispatched_before_proactive_compression(monkeypatch):
    """Regression test for a real failure: when the context budget is "nearly
    exceeded", the bridge used to compress older tool messages and retry the
    upstream call *before* dispatching the CURRENT round's tool calls. That
    left the just-appended assistant `tool_calls` message with no matching
    `tool` replies -- an invalid OpenAI-style conversation that strict
    providers (observed with Minimax, error code 2013 "invalid params") reject
    outright with a 400, surfacing as an opaque failure to the bridge's caller.

    This reproduces the exact shape: rounds 1-4 are each a separate, complete
    tool-calling turn -- enough whole ROUNDS for `_compress_tool_context`
    (round-based; keeps the most recent 3 by default) to have something to
    compress. Round 5 issues one more tool call and reports high enough
    `prompt_tokens` to cross the "nearly exceeded" (70%) budget threshold
    without crossing the hard limit. The fix must dispatch round 5's tool
    call (and append its result) *before* compressing older rounds and
    moving on -- never leave it dangling.
    """
    max_ctx = chat_completion_module.get_max_context_tokens("test")
    nearly_exceeded_prompt_tokens = int(max_ctx * 0.7) + 100
    assert nearly_exceeded_prompt_tokens < max_ctx, "test assumption: nearly-exceeded but not hard-exceeded"

    final_round_call_id = "call_final_round"

    fake_client = FakeClient(
        [
            *(FakeResponse(200, _multi_tool_calls_response([(f"call_r{r}", "search", f'{{"query": "q{r}"}}')])) for r in range(4)),
            FakeResponse(
                200,
                _multi_tool_calls_response(
                    [(final_round_call_id, "search", '{"query": "one more"}')],
                    prompt_tokens=nearly_exceeded_prompt_tokens,
                ),
            ),
            FakeResponse(200, _stop_response("Final answer after compression.")),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    async def fake_call_tools(tool_calls, **kwargs):
        return [
            CallToolResult(
                content=[TextContent(type="text", text=f"result for {name} {arguments}")],
                isError=False,
            )
            for name, arguments in tool_calls
        ]

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "call_tools", fake_call_tools)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "do a lot of research"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    assert response.choices[0].message.content == "Final answer after compression."
    # Six upstream calls: rounds 1-4, round 5, and the final answer.
    assert len(fake_client.posts) == 6

    # Compression must actually have happened (proving this test exercises the
    # code path it claims to, not passing vacuously because nothing triggered).
    assert any(
        "[Earlier tool results summarized to save context]" in str(m.get("content"))
        for m in fake_client.posts[-1][1]["json"]["messages"]
        if m.get("role") == "user"
    )

    # The LAST upstream call's request is what would have been rejected by a
    # strict provider: it must NOT contain round 5's assistant `tool_calls`
    # message without a matching `tool` reply for `final_round_call_id`.
    last_request_messages = fake_client.posts[-1][1]["json"]["messages"]

    final_round_assistant_index = next(
        i
        for i, m in enumerate(last_request_messages)
        if m.get("role") == "assistant"
        and any(tc.get("id") == final_round_call_id for tc in (m.get("tool_calls") or []))
    )
    subsequent_tool_call_ids = {
        m.get("tool_call_id") for m in last_request_messages[final_round_assistant_index + 1 :] if m.get("role") == "tool"
    }
    assert final_round_call_id in subsequent_tool_call_ids, (
        "round 5's tool call must be answered before any further upstream call -- an "
        "unanswered `tool_calls` message is an invalid conversation for strict providers"
    )
    # It's the REAL dispatched result (not just a placeholder), proving dispatch
    # happened before compression ran.
    real_reply = next(
        m
        for m in last_request_messages[final_round_assistant_index + 1 :]
        if m.get("role") == "tool" and m.get("tool_call_id") == final_round_call_id
    )
    assert "result for search" in str(real_reply.get("content"))


def test_get_retry_after_seconds_parses_header_and_body():
    """The Retry-After hint is parsed from the HTTP header and the JSON body,
    and is capped at the configured maximum."""
    # From the HTTP header.
    resp = FakeResponse(429, "{}", headers={"Retry-After": "60"})
    assert chat_completion_module._get_retry_after_seconds(resp) == 60.0

    # From the JSON error body (OpenRouter-style).
    resp2 = FakeResponse(
        429,
        '{"error": {"metadata": {"retry_after_seconds": 30}}}',
    )
    assert chat_completion_module._get_retry_after_seconds(resp2) == 30.0

    # Capped at the maximum.
    resp3 = FakeResponse(429, "{}", headers={"Retry-After": "9999"})
    assert chat_completion_module._get_retry_after_seconds(resp3) == chat_completion_module.MAX_UPSTREAM_RETRY_AFTER_SECONDS

    # No hint -> 0.0.
    resp4 = FakeResponse(200, "{}")
    assert chat_completion_module._get_retry_after_seconds(resp4) == 0.0


def test_chat_completions_synthesizes_fallback_when_upstream_fails_after_evidence(monkeypatch):
    """Regression test: when the upstream fails (transient 502) AFTER tool
    evidence has been gathered, the bridge must synthesize a valid fallback
    response — not crash with a 500 from an invalid synthetic response."""
    fake_client = FakeClient(
        [
            # Turn 1: model issues a tool call.
            FakeResponse(200, _tool_calls_response("search", '{"query": "hello"}')),
            # Turn 2: upstream is overloaded (transient 502 in a 200 body).
            FakeResponse(200, '{"error": {"message": "Upstream error from Nvidia: Service temporarily overloaded", "code": 502}}'),
            # Turn 3: the synthesis request also fails (still overloaded).
            FakeResponse(200, '{"error": {"message": "Upstream error from Nvidia: Service temporarily overloaded", "code": 502}}'),
            FakeResponse(200, '{"error": {"message": "Upstream error from Nvidia: Service temporarily overloaded", "code": 502}}'),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    async def fake_call_tools(tool_calls, **kwargs):
        return [
            CallToolResult(
                content=[TextContent(type="text", text="result for search")],
                isError=False,
            )
            for name, _ in tool_calls
        ]

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "call_tools", fake_call_tools)
    # Avoid the real 2s retry delay in tests.
    monkeypatch.setattr(chat_completion_module, "DEFAULT_UPSTREAM_RETRY_DELAY_SECONDS", 0.0)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "do a search"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    # Must NOT raise (previously this crashed with a pydantic ValidationError -> 500).
    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    # A fallback response was synthesized from the tool evidence.
    assert response.choices[0].message.content
    assert "result for search" in response.choices[0].message.content


def test_chat_completions_recovers_cohere_style_error_finish_reason_end_to_end(monkeypatch):
    """Regression test for a real production failure: Cohere (via
    OpenRouter) returns HTTP 200 with `finish_reason: "error"` -- not one of
    the OpenAI spec's five standard values -- when its own generation fails
    mid-stream. Previously `chat_completions` raised a 502 ("Failed to parse
    upstream chat completion response"), discarding the real signal in the
    payload (usage, a `reasoning` field). This drives the full public
    `chat_completions` entry point end to end and checks it now degrades
    gracefully instead of raising.
    """
    cohere_error_body = json.dumps(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 0,
            "model": "cohere/north-mini-code:free",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "error",
                    "native_finish_reason": "error",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "Let me start by searching for information about this topic.",
                    },
                }
            ],
            "usage": {"prompt_tokens": 41250, "completion_tokens": 341, "total_tokens": 41591},
        }
    )
    fake_client = FakeClient([FakeResponse(200, cohere_error_body)])

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "cohere/north-mini-code:free",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    # Must NOT raise -- the old behavior was a 502 HTTPException here.
    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    # Degraded gracefully (empty-content fallback), not a hard failure.
    assert response.choices[0].message.content
    assert response.choices[0].finish_reason.value == "stop"


def test_chat_completions_synthesis_retries_transient_error_body(monkeypatch):
    """Regression test: when the synthesis request hits a transient HTTP 200
    with an error body (e.g. `{"error": {"message": "A Timeout Occurred",
    "code": 504}}` or `provider_unavailable`), the bridge must retry it like
    the main loop does -- not fail with `4 validation errors for
    CreateChatCompletionResponse` and lose the whole synthesis."""
    fake_client = FakeClient(
        [
            # Turn 1: model issues a tool call.
            FakeResponse(200, _tool_calls_response("search", '{"query": "hello"}')),
            # Turn 2: model produces a final answer (tool loop completes).
            FakeResponse(200, _stop_response("The final answer.")),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    async def fake_call_tools(tool_calls, **kwargs):
        return [
            CallToolResult(
                content=[TextContent(type="text", text="result for search")],
                isError=False,
            )
            for name, _ in tool_calls
        ]

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "call_tools", fake_call_tools)
    monkeypatch.setattr(chat_completion_module, "DEFAULT_UPSTREAM_RETRY_DELAY_SECONDS", 0.0)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "do a search"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    # Directly exercise the synthesis retry helper: a transient error body is
    # retried, then a valid response is returned.
    class FakeSynthClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, **kwargs):
            self.posts.append(url)
            if len(self.posts) == 1:
                return FakeResponse(200, '{"error": {"message": "A Timeout Occurred", "code": 504}}')
            return FakeResponse(200, _stop_response("Synthesized after retry."))

    synth_client = FakeSynthClient()
    synth_request = chat_completion_module._build_synthesis_request(
        request,
        stop_reason="repeated_tool_calls",
        request_messages=request.messages,
        force_answer=True,
    )
    text = asyncio.run(chat_completion_module._post_synthesis_with_retry(synth_client, synth_request))

    # The transient error was retried once, then the valid response was used.
    assert len(synth_client.posts) == 2
    assert text is not None
    response = chat_completion_module.CreateChatCompletionResponse.model_validate_json(text)
    assert response.choices[0].message.content == "Synthesized after retry."


def test_chat_completions_synthesis_returns_none_after_all_retries_fail(monkeypatch):
    """When every synthesis attempt returns a transient error body, the helper
    returns None (so the caller falls back to the deterministic evidence dump)
    instead of raising a validation error."""
    fake_client = FakeClient(
        [
            FakeResponse(200, '{"error": {"message": "A Timeout Occurred", "code": 504}}'),
            FakeResponse(200, '{"error": {"message": "A Timeout Occurred", "code": 504}}'),
            FakeResponse(200, '{"error": {"message": "A Timeout Occurred", "code": 504}}'),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "DEFAULT_UPSTREAM_RETRY_DELAY_SECONDS", 0.0)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    synth_request = chat_completion_module._build_synthesis_request(
        request,
        stop_reason="repeated_tool_calls",
        request_messages=request.messages,
        force_answer=True,
    )
    text = asyncio.run(chat_completion_module._post_synthesis_with_retry(fake_client, synth_request))

    # 1 initial + 2 retries = 3 posts, all transient -> None.
    assert len(fake_client.posts) == 3
    assert text is None


def test_synthesis_recovers_cohere_style_error_finish_reason(monkeypatch):
    """Regression test: when the synthesis request returns a well-formed
    payload with `finish_reason: "error"` (Cohere via OpenRouter), the
    synthesis path must recover it (coerce to 'stop') instead of failing with
    a `choices.0.finish_reason` enum validation error and losing the whole
    synthesis."""
    cohere_error_body = json.dumps(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 0,
            "model": "cohere/north-mini-code:free",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "error",
                    "native_finish_reason": "error",
                    "message": {
                        "role": "assistant",
                        "content": "Synthesized answer despite the error finish reason.",
                        "reasoning": "Let me start by searching for information about this topic.",
                    },
                }
            ],
            "usage": {"prompt_tokens": 41250, "completion_tokens": 341, "total_tokens": 41591},
        }
    )

    # `_parse_synthesis_response` must recover the Cohere-style error body.
    response = chat_completion_module._parse_synthesis_response(cohere_error_body)
    assert response is not None
    assert response.choices[0].message.content == "Synthesized answer despite the error finish reason."
    assert response.choices[0].finish_reason.value == "stop"

    # A genuinely malformed body (not just a bad finish_reason) returns None.
    assert chat_completion_module._parse_synthesis_response('{"not": "a completion"}') is None


def test_chat_completions_falls_back_model_on_agentic_harness_403(monkeypatch):
    """Regression test: OpenRouter returns HTTP 403 with "only available on
    agentic harnesses" for some `:free` models (e.g.
    `thinkingmachines/inkling:free`). Previously this caused a hard 502 to the
    client. The bridge must instead fall back to a different model and retry,
    so the request succeeds."""
    fake_client = FakeClient(
        [
            # First request: 403 agentic-harness gate.
            FakeResponse(
                403,
                '{"error": {"message": "thinkingmachines/inkling:free is only available on agentic harnesses. Try plugging it into a coding agent or productivity app listed on https://openrouter.ai/apps", "code": 403}}',
            ),
            # Second request (fallback model): success.
            FakeResponse(200, _stop_response("Recovered with fallback model.")),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    # Force a deterministic fallback model.
    monkeypatch.setattr(
        chat_completion_module,
        "_resolve_fallback_model",
        lambda request: "openrouter/auto-beta",
    )

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "thinkingmachines/inkling:free",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    # The 403 was handled by falling back to a different model, then succeeded.
    assert len(fake_client.posts) == 2
    assert response.choices[0].message.content == "Recovered with fallback model."


def test_chat_completions_agentic_harness_403_no_fallback_raises(monkeypatch):
    """When no fallback model is available, the agentic-harness 403 still
    raises a clean 502 (not an unhandled exception)."""
    fake_client = FakeClient(
        [
            FakeResponse(
                403,
                '{"error": {"message": "thinkingmachines/inkling:free is only available on agentic harnesses", "code": 403}}',
            ),
        ]
    )

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "_resolve_fallback_model", lambda request: None)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "thinkingmachines/inkling:free",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    try:
        asyncio.run(chat_completion_module.chat_completions(request, None))
        assert False, "expected an HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 502


def test_hard_context_budget_compresses_and_still_dispatches_tool_calls(monkeypatch):
    """Regression test for the exact abort seen in production:

        finish reason: tool_calls; tool_calls=True
        (15ms later) tool loop context budget exceeded (99224 > 96000 tokens);
                    stopping tool loop and synthesizing a final answer

    ``usage.prompt_tokens`` describes the prompt sent at the TOP of the turn --
    it EXCLUDES the tool results appended since. So the hard-budget check fired
    on a stale count and then threw away tool calls the model had just asked
    for, ending a long research run in degraded synthesis while there was still
    compressible history available.

    Correct behavior: compress the older, already-answered rounds and CONTINUE
    (dispatching the current round's tool calls), instead of stopping.
    """
    max_ctx = chat_completion_module.get_max_context_tokens("test")
    over_budget_prompt_tokens = max_ctx + 500
    final_round_call_id = "call_over_budget"

    fake_client = FakeClient(
        [
            # Enough complete rounds that compression has something to compress.
            *(
                FakeResponse(
                    200,
                    _multi_tool_calls_response([(f"call_r{r}", "search", f'{{"query": "q{r}"}}')]),
                )
                for r in range(5)
            ),
            # This turn reports a prompt OVER the hard budget AND issues a tool call.
            FakeResponse(
                200,
                _multi_tool_calls_response(
                    [(final_round_call_id, "search", '{"query": "final angle"}')],
                    prompt_tokens=over_budget_prompt_tokens,
                ),
            ),
            FakeResponse(200, _stop_response("Final answer after budget rescue.")),
        ]
    )

    dispatched: list[tuple[str, str]] = []

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    async def fake_call_tools(tool_calls, **kwargs):
        dispatched.extend(tool_calls)
        return [
            CallToolResult(
                content=[TextContent(type="text", text=f"result for {name} {arguments}")],
                isError=False,
            )
            for name, arguments in tool_calls
        ]

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)
    monkeypatch.setattr(chat_completion_module, "call_tools", fake_call_tools)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "research a lot"}],
            "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        }
    )

    response = asyncio.run(chat_completion_module.chat_completions(request, None))

    # The model's own final answer won -- NOT a degraded evidence dump.
    assert response.choices[0].message.content == "Final answer after budget rescue."

    # The over-budget round's tool call was actually DISPATCHED rather than
    # discarded (this is the core regression: it used to be dropped).
    assert any(
        arguments and "final angle" in arguments for _name, arguments in dispatched
    ), f"over-budget tool call was not dispatched; dispatched={dispatched}"

    # Compression genuinely ran (so this is not passing vacuously).
    assert any(
        "[Earlier tool results summarized to save context]" in str(m.get("content"))
        for _url, kwargs in fake_client.posts
        for m in kwargs["json"]["messages"]
        if m.get("role") == "user"
    )
