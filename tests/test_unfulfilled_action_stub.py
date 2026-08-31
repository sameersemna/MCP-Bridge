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

from mcp_bridge.openai_clients.chatCompletion import _looks_like_unfulfilled_action_stub


def test_detects_the_reported_islamqa_stub():
    # The exact failure mode reported: a bare intent announcement with no
    # attached tool call and no real content, returned as if it were a
    # complete final answer.
    assert _looks_like_unfulfilled_action_stub(
        "Let me fetch the islamqa.info fatwa about opening mosques with ceremonies."
    ) is True


def test_detects_various_intent_phrasings():
    stubs = [
        "I'll search for relevant sources on this topic.",
        "I need to check the official documentation first.",
        "Let me look up the latest pricing page.",
        "I will verify this claim before answering.",
        "Let's check the referenced fatwa.",
    ]
    for text in stubs:
        assert _looks_like_unfulfilled_action_stub(text) is True, text


def test_does_not_flag_a_real_long_answer_with_similar_opening():
    # Same opening phrase, but followed by substantial real content -- must
    # NOT be treated as a stub.
    long_answer = (
        "Let me summarize what I found. " + ("This is a detailed finding. " * 20)
    )
    assert _looks_like_unfulfilled_action_stub(long_answer) is False


def test_does_not_flag_unrelated_short_answers():
    assert _looks_like_unfulfilled_action_stub("Yes, that is permissible according to the majority view.") is False
    assert _looks_like_unfulfilled_action_stub("Let me know if you have further questions.") is False


def test_empty_text_is_not_a_stub():
    assert _looks_like_unfulfilled_action_stub("") is False
    assert _looks_like_unfulfilled_action_stub("   ") is False
