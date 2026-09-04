"""Coverage for the tool-discovery/no-tool-call log lines.

These lines exist so that "why didn't the model call tool X" is answerable
directly from the CLI log stream (e.g. `docker logs`), instead of requiring
someone to open the per-request trace JSON file or exec into the running
container to list tools by hand.
"""

import asyncio
import json
from contextlib import asynccontextmanager

from loguru import logger

from mcp_bridge.openai_clients import chatCompletion as chat_completion_module
from mcp_bridge.openai_clients.utils import tool_names

# `CreateChatCompletionRequest` is intentionally read off the already-imported
# `chat_completion_module` rather than via a fresh top-level `from
# lmos_openai_types import ...` here. tests/test_diagnostic_snippet.py replaces
# `sys.modules["lmos_openai_types"]` with a fake stub module and never restores
# it, which would otherwise poison a fresh import in any test file collected
# after it (alphabetically, this one).
CreateChatCompletionRequest = chat_completion_module.CreateChatCompletionRequest


class _CapturingSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message) -> None:
        self.messages.append(message.record["message"])


def _capture_logs():
    sink = _CapturingSink()
    handler_id = logger.add(sink.write, level="DEBUG")
    return sink, handler_id


def test_tool_names_extracts_function_name_from_dict_and_object_shaped_tools():
    class _Function:
        def __init__(self, name):
            self.name = name

    dict_shaped = type("Tool", (), {"function": {"name": "google_weather"}})()
    object_shaped = type("Tool", (), {"function": _Function("google_search")})()
    missing_name = type("Tool", (), {"function": {}})()

    assert tool_names([dict_shaped, object_shaped, missing_name]) == [
        "google_weather",
        "google_search",
    ]


def test_tool_names_handles_empty_or_none_input():
    assert tool_names(None) == []
    assert tool_names([]) == []


class FakeResponse:
    def __init__(self, status_code: int, text: str, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)

    async def post(self, url: str, **kwargs):
        return self._responses.pop(0)


def _stop_response(content: str) -> str:
    return json.dumps(
        {
            "id": "gen-1",
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
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def test_chat_completions_logs_discovered_tool_names(monkeypatch):
    fake_client = FakeClient([FakeResponse(200, _stop_response("Direct answer."))])

    @asynccontextmanager
    async def fake_get_client(request=None):
        yield fake_client

    monkeypatch.setattr(chat_completion_module, "get_client", fake_get_client)

    request = CreateChatCompletionRequest.model_validate(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "what's the weather in berlin"}],
            "tools": [
                {"type": "function", "function": {"name": "google_search", "parameters": {}}},
                {"type": "function", "function": {"name": "google_weather", "parameters": {}}},
            ],
        }
    )

    sink, handler_id = _capture_logs()
    try:
        asyncio.run(chat_completion_module.chat_completions(request, None))
    finally:
        logger.remove(handler_id)

    discovery_lines = [m for m in sink.messages if m.startswith("tools discovered:")]
    assert len(discovery_lines) == 1
    assert "2 available: google_search, google_weather" in discovery_lines[0]

    no_tool_call_lines = [m for m in sink.messages if m.startswith("no tool calls found")]
    assert len(no_tool_call_lines) == 1
    assert "finish_reason=stop" in no_tool_call_lines[0]
    assert "2 tool(s) were available to the model" in no_tool_call_lines[0]
