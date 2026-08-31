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
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


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
