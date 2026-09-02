"""End-to-end tests for the `chat_completions` tool loop.

These drive the *public* `chat_completions` entry point (not just its private
helpers) with a fake upstream inference server and a fake tool executor, so the
full tool-calling state machine is exercised: tool-call dispatch, tool-result
injection, and final-answer synthesis.
"""

import asyncio
import json
from contextlib import asynccontextmanager

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
        return self._responses.pop(0)


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

    This reproduces the exact shape: round 1 dispatches enough tool calls to
    give `_compress_tool_context` something to compress (more than its
    `keep_recent=6`); round 2 issues one more tool call and reports high
    enough `prompt_tokens` to cross the "nearly exceeded" (70%) threshold
    without crossing the hard limit. The fix must dispatch round 2's tool
    call (and append its result) *before* compressing and moving on.
    """
    round1_calls = [(f"call_{i}", "search", f'{{"query": "q{i}"}}') for i in range(8)]
    round2_call_id = "call_round2"

    max_ctx = chat_completion_module.get_max_context_tokens("test")
    nearly_exceeded_prompt_tokens = int(max_ctx * 0.7) + 100
    assert nearly_exceeded_prompt_tokens < max_ctx, "test assumption: nearly-exceeded but not hard-exceeded"

    fake_client = FakeClient(
        [
            FakeResponse(200, _multi_tool_calls_response(round1_calls)),
            FakeResponse(
                200,
                _multi_tool_calls_response(
                    [(round2_call_id, "search", '{"query": "one more"}')],
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
    # Three upstream calls: round 1, round 2, and the final answer.
    assert len(fake_client.posts) == 3

    # The THIRD upstream call's request is what would have been rejected by a
    # strict provider: it must NOT contain round 2's assistant `tool_calls`
    # message without a matching `tool` reply for `call_round2`.
    third_request_messages = fake_client.posts[2][1]["json"]["messages"]

    round2_assistant_index = next(
        i
        for i, m in enumerate(third_request_messages)
        if m.get("role") == "assistant"
        and any(tc.get("id") == round2_call_id for tc in (m.get("tool_calls") or []))
    )
    subsequent_tool_call_ids = {
        m.get("tool_call_id") for m in third_request_messages[round2_assistant_index + 1 :] if m.get("role") == "tool"
    }
    assert round2_call_id in subsequent_tool_call_ids, (
        "round 2's tool call must be answered (dispatched or placeholder) before any further "
        "upstream call -- an unanswered `tool_calls` message is an invalid conversation for "
        "strict providers"
    )
    # It's the REAL dispatched result (not just a placeholder), proving dispatch
    # happened before compression ran.
    real_reply = next(
        m
        for m in third_request_messages[round2_assistant_index + 1 :]
        if m.get("role") == "tool" and m.get("tool_call_id") == round2_call_id
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
