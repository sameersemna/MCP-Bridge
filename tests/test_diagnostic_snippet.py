import json
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

from mcp_bridge.openai_clients.chatCompletion import _diagnostic_snippet


def _build_triple_nested_openrouter_error(real_message: str) -> str:
    """Build a body shaped like OpenRouter's real passthrough of a Minimax 400:
    error -> metadata.raw (JSON string) -> details (JSON string) -> error.message.
    """
    level2 = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": real_message}})
    level1 = json.dumps(
        {"error": {"message": "Backend request failed with status 400", "type": "backend_error", "code": 400, "details": level2}}
    )
    return json.dumps({"error": {"message": "Provider returned error", "code": 400, "metadata": {"raw": level1}}})


def test_unwraps_triple_nested_json_error_to_the_real_message():
    real_message = "messages: roles must alternate between user/assistant after the system message"
    body = _build_triple_nested_openrouter_error(real_message)

    # A naive text[:300] (or even the whitespace-collapsed predecessor) never
    # reaches the real message: escaping overhead from 3 nesting levels eats
    # the whole budget.
    assert real_message not in body[:300]

    snippet = _diagnostic_snippet(body)
    assert real_message in snippet
    # The outer context is preserved too, so the chain of wrapping is visible.
    assert "Provider returned error" in snippet
    assert "Backend request failed with status 400" in snippet


def test_plain_text_body_falls_back_to_truncation():
    assert _diagnostic_snippet("plain text error, not JSON at all") == "plain text error, not JSON at all"


def test_json_without_message_fields_falls_back_to_truncation():
    body = json.dumps({"foo": "bar", "baz": [1, 2, 3]})
    assert _diagnostic_snippet(body) == body


def test_empty_body():
    assert _diagnostic_snippet("") == "<empty body>"
    assert _diagnostic_snippet("   \n  ") == "<empty body>"


def test_whitespace_padding_before_real_content_is_collapsed():
    padded = (" " * 400) + "real error content here"
    assert _diagnostic_snippet(padded) == "real error content here"


def test_long_unwrapped_chain_is_truncated_with_ellipsis():
    real_message = "x" * 1000
    body = _build_triple_nested_openrouter_error(real_message)
    snippet = _diagnostic_snippet(body, limit=50)
    assert len(snippet) == 50
    assert snippet.endswith("…")
