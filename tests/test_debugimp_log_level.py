"""Coverage for the DEBUGIMP log level.

DEBUGIMP (severity 15, between INFO=20 and DEBUG=10) exists so an operator can
set `log_level: "DEBUGIMP"` in config.json and see low-frequency, high-value
debug summaries (tool calls, finish reasons, turn boundaries) without the
per-SSE-chunk firehose (one "stream event received" / "should_forward" /
"forwarding message" triplet per streamed token) that full DEBUG produces.
"""

import asyncio
import json

import pytest
from loguru import logger

import mcp_bridge.logging as logging_module
from mcp_bridge.config.final import Logging
from mcp_bridge.openai_clients import streamChatCompletion as stream_module

CreateChatCompletionRequest = stream_module.CreateChatCompletionRequest


class _CapturingSink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, message) -> None:
        self.messages.append(message.record["message"])


def test_debugimp_level_is_registered_between_debug_and_info():
    level = logger.level("DEBUGIMP")
    assert logger.level("DEBUG").no < level.no < logger.level("INFO").no


def test_logging_config_accepts_debugimp_and_rejects_unknown_level():
    assert Logging(log_level="DEBUGIMP").log_level == "DEBUGIMP"
    with pytest.raises(Exception):
        Logging(log_level="NOT_A_LEVEL")


def test_sink_at_debugimp_level_hides_plain_debug_but_keeps_debugimp():
    sink = _CapturingSink()
    handler_id = logger.add(sink.write, level="DEBUGIMP")
    try:
        logger.debug("plain debug noise, should be hidden")
        logger.log("DEBUGIMP", "important debug summary, should be visible")
        logger.info("plain info, should be visible")
    finally:
        logger.remove(handler_id)

    assert "plain debug noise, should be hidden" not in sink.messages
    assert "important debug summary, should be visible" in sink.messages
    assert "plain info, should be visible" in sink.messages


# --- Minimal self-contained SSE fakes (mirrors tests/test_stream_chat_completion.py) ---


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
    def __init__(self, chunks: list[str]):
        self._chunks = chunks
        self.response = _FakeResponse()

    async def aiter_sse(self):
        for chunk in self._chunks:
            yield _FakeSSEEvent(chunk)


class _FakeSSEConnectContextManager:
    def __init__(self, chunks: list[str]):
        self._chunks = chunks

    async def __aenter__(self):
        return _FakeEventSource(self._chunks)

    async def __aexit__(self, *exc_info):
        return False


class _FakeClientContextManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


async def _fake_add_tools(request):
    request.tools = []
    return request


def _patch_stream(monkeypatch, chunks: list[str]):
    def fake_aconnect_sse(client, method, url, content=None):
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


def test_streaming_debugimp_sink_suppresses_per_chunk_noise(monkeypatch):
    # Three streamed content chunks followed by a "stop" chunk -- at full
    # DEBUG this produces 3x ("stream event received" + "should_forward" +
    # "forwarding message") = 9 lines, plus one more "stream event received"
    # for the stop chunk. A DEBUGIMP sink should show none of that, only the
    # low-frequency turn-boundary summaries.
    chunks = [
        json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": {"content": f"chunk{i}"}, "finish_reason": None}],
            }
        )
        for i in range(3)
    ]
    chunks.append(
        json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
    )
    _patch_stream(monkeypatch, chunks)

    sink = _CapturingSink()
    handler_id = logger.add(sink.write, level="DEBUGIMP")
    try:
        results = _run_stream(_make_request())
    finally:
        logger.remove(handler_id)

    assert len(results) >= 3  # the 3 forwarded content chunks were still produced

    noisy_prefixes = ("stream event received", "should_forward=", "forwarding message")
    assert not any(m.startswith(noisy_prefixes) for m in sink.messages)

    assert any(m.startswith("tools discovered:") for m in sink.messages)
    assert any(m.startswith("no tool calls found") for m in sink.messages)
    assert any(m == "sending final event" for m in sink.messages)
