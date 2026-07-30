import time

from mcp_bridge.openai_clients.chatCompletion import DEFAULT_MAX_TOOL_TURNS, _record_timing, should_continue_tool_loop


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
