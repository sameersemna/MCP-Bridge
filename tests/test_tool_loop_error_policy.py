import sys
import types
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

fake_lmos = types.ModuleType("lmos_openai_types")


class FinishReason1(str, Enum):
    stop = "stop"
    length = "length"
    tool_calls = "tool_calls"
    content_filter = "content_filter"
    function_call = "function_call"


class ChatCompletionRequestMessage:
    def __init__(self, role=None, content=None, tool_calls=None, tool_call_id=None, **kwargs):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.root = SimpleNamespace(role=role, content=content)

    @classmethod
    def model_validate(cls, payload):
        if isinstance(payload, cls):
            return payload
        if isinstance(payload, dict):
            return cls(**payload)
        return cls(role="assistant", content=str(payload))


class CreateChatCompletionRequest:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class CreateChatCompletionResponse:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


fake_lmos.ChatCompletionRequestMessage = ChatCompletionRequestMessage
fake_lmos.CreateChatCompletionRequest = CreateChatCompletionRequest
fake_lmos.CreateChatCompletionResponse = CreateChatCompletionResponse
fake_lmos.FinishReason1 = FinishReason1
sys.modules["lmos_openai_types"] = fake_lmos

fake_opentelemetry = types.ModuleType("opentelemetry")
fake_trace_module = types.ModuleType("opentelemetry.trace")


class _FakeSpan:
    def __init__(self, *args, **kwargs):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, *args, **kwargs):
        return None


class _FakeTracer:
    def start_as_current_span(self, *args, **kwargs):
        return self

    def __enter__(self):
        return _FakeSpan()

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStatusCode:
    ERROR = "ERROR"


class _FakeStatus:
    def __init__(self, code, description=None):
        self.code = code
        self.description = description


fake_trace_module.get_tracer = lambda *args, **kwargs: _FakeTracer()
fake_trace_module.Status = _FakeStatus
fake_trace_module.StatusCode = _FakeStatusCode
fake_opentelemetry.trace = fake_trace_module
sys.modules["opentelemetry"] = fake_opentelemetry
sys.modules["opentelemetry.trace"] = fake_trace_module

from mcp_bridge.openai_clients.chatCompletion import _should_stop_tool_loop_on_tool_errors


def test_timeout_errors_with_partial_evidence_do_not_stop_loop():
    request_messages = [
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "Search results gathered from a prior successful call"}],
                "tool_call_id": "call_1",
            }
        )
    ]

    assert _should_stop_tool_loop_on_tool_errors(
        [f"fetch_content: Timeout Error calling fetch_content"] * 5,
        request_messages,
    ) is False


def test_validation_error_without_evidence_does_not_stop_loop():
    # A validation error on the FIRST tool call (no evidence yet) is recoverable:
    # the model can correct its arguments. The loop must NOT stop early.
    assert _should_stop_tool_loop_on_tool_errors(
        ["sequential-thinking: Input validation error: Invalid arguments for tool sequentialthinking"],
        [],
    ) is False


def test_fatal_error_stops_loop_even_without_evidence():
    # "No MCP client found" / "Unknown tool" cannot be fixed by the model, so
    # the loop should stop rather than spin.
    assert _should_stop_tool_loop_on_tool_errors(
        ["google-search: No MCP client found for tool 'google-search'"],
        [],
    ) is True


def test_too_many_recoverable_errors_stop_loop():
    # More than 3 recoverable errors -> stop to avoid an infinite loop.
    assert _should_stop_tool_loop_on_tool_errors(
        ["sequential-thinking: Input validation error"] * 4,
        [],
    ) is True
