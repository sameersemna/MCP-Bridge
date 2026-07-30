import time

from lmos_openai_types import ChatCompletionRequestMessage, CreateChatCompletionResponse, FinishReason1

from mcp_bridge.openai_clients.chatCompletion import (
    DEFAULT_MAX_TOOL_TURNS,
    _build_tool_loop_stop_response,
    _format_tool_loop_stop_message,
    _record_timing,
    should_continue_tool_loop,
)


class DummyTraceLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(self, event_type: str, **payload: object) -> None:
        self.events.append({"type": event_type, **payload})


def test_should_continue_tool_loop_when_under_limit():
    assert should_continue_tool_loop("tool_calls", tool_call_count=1, iteration_count=2, max_tool_turns=3) is True


def test_should_stop_tool_loop_when_limit_reached():
    assert should_continue_tool_loop("tool_calls", tool_call_count=1, iteration_count=3, max_tool_turns=3) is False


def test_should_stop_tool_loop_for_non_tool_finish_reason_without_tool_calls():
    assert should_continue_tool_loop("stop", tool_call_count=0, iteration_count=1, max_tool_turns=3) is False


def test_should_continue_tool_loop_for_tool_call_finish_reason():
    assert should_continue_tool_loop("tool_calls", tool_call_count=1, iteration_count=1, max_tool_turns=3) is True


def test_should_continue_tool_loop_when_tool_calls_are_present_even_if_finish_reason_is_stop():
    assert should_continue_tool_loop("stop", tool_call_count=1, iteration_count=1, max_tool_turns=3) is True


def test_default_tool_turn_limit_supports_multi_step_tool_workflows():
    assert DEFAULT_MAX_TOOL_TURNS >= 5
    assert should_continue_tool_loop("tool_calls", tool_call_count=1, iteration_count=4, max_tool_turns=DEFAULT_MAX_TOOL_TURNS) is True


def test_record_timing_emits_elapsed_ms():
    trace_logger = DummyTraceLogger()
    _record_timing(trace_logger, "tool_dispatch", time.perf_counter() - 0.01)

    assert trace_logger.events[0]["type"] == "timing"
    assert trace_logger.events[0]["stage"] == "tool_dispatch"
    assert trace_logger.events[0]["elapsed_ms"] >= 0


def test_format_tool_loop_stop_message_includes_turns_and_limit():
    message = _format_tool_loop_stop_message(tool_turns_completed=3, max_tool_turns=12)

    assert message == "stopping tool loop after 3 turn(s); max_tool_turns=12"


def test_build_tool_loop_stop_response_replaces_tool_calls_with_summary():
    response = CreateChatCompletionResponse.model_validate(
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_test",
                                "type": "function",
                                "function": {"name": "google_search", "arguments": '{"query": "test"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "created": 1,
            "model": "test-model",
            "object": "chat.completion",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    request_messages = [
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "partial search results"}],
                "tool_call_id": "call_test",
            }
        )
    ]

    stop_response = _build_tool_loop_stop_response(
        response,
        stop_reason="max_tool_turns",
        request_messages=request_messages,
    )

    assert stop_response.choices[0].message.content is not None
    assert "partial search results" in stop_response.choices[0].message.content
    assert stop_response.choices[0].message.tool_calls is None
    assert stop_response.choices[0].finish_reason == FinishReason1.stop
