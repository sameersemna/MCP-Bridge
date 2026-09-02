import asyncio
import time

from lmos_openai_types import ChatCompletionRequestMessage, CreateChatCompletionRequest, CreateChatCompletionResponse, FinishReason1

from mcp_bridge.mcp_clients.AbstractClient import CallToolResult, GenericMcpClient, TextContent

from mcp_bridge.openai_clients.chatCompletion import (
    DEFAULT_MAX_TOOL_TURNS,
    _build_empty_content_response,
    _build_synthesis_request,
    _build_synthetic_tool_calls,
    _build_tool_loop_stop_response,
    _compress_tool_context,
    _context_budget_exceeded,
    _context_budget_nearly_exceeded,
    _degraded_marker_comment,
    _detect_repeated_tool_calls,
    _extract_message_text,
    _extract_tool_calls,
    _extract_tool_message_text,
    _finalize_degraded_response,
    _finalize_recovered_response,
    _format_tool_loop_stop_message,
    _group_tool_rounds,
    _has_only_weak_tool_evidence,
    _parse_pseudo_tool_calls,
    _record_and_raise_upstream_failure,
    _record_timing,
    _should_stop_tool_loop_on_tool_errors,
    _should_use_empty_content_fallback,
    _strip_pseudo_tool_call_markers,
    get_max_context_tokens,
    get_max_tool_turns,
    should_continue_tool_loop,
)


class DummyTraceLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.path = "logs/dummy-trace.json"

    def record(self, event_type: str, **payload: object) -> None:
        self.events.append({"type": event_type, **payload})


def test_call_tool_retries_once_after_timeout(monkeypatch):
    attempts = 0

    class DummySession:
        async def call_tool(self, name: str, arguments: dict[str, object]):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.TimeoutError()
            return CallToolResult(content=[TextContent(type="text", text="ok")], isError=False)

    class DummyClient(GenericMcpClient):
        async def _maintain_session(self) -> None:
            # Re-establish the session after a reset, then block like the real
            # maintainer so the _session_maintainer loop does not null it.
            self.session = DummySession()
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

    async def run_test() -> None:
        client = DummyClient("dummy")
        client.session = DummySession()
        # Start the client so _reset_session will restart the maintainer after
        # a timeout, which re-establishes the session for the retry.
        await client.start()

        # Use a real (short) sleep. Monkeypatching asyncio.sleep to a no-op
        # makes the background _session_maintainer loop spin at 100% CPU and
        # the test never completes. The retry delay is only 0.25s, so a real
        # sleep keeps the test fast while letting the maintainer actually idle.
        monkeypatch.setenv("MCP_BRIDGE_TOOL_RETRY_DELAY_SECONDS", "0.01")

        result = await client.call_tool("search", {"query": "test"}, timeout=1)

        assert attempts == 2
        assert result.isError is False
        assert result.content[0].text == "ok"

    asyncio.run(run_test())


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


def test_get_max_tool_turns_clamps_too_low_environment_values(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_MAX_TOOL_TURNS", "4")

    assert get_max_tool_turns() == DEFAULT_MAX_TOOL_TURNS


def test_get_max_context_tokens_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("MCP_BRIDGE_CONTEXT_BUDGET_FRACTION", raising=False)
    monkeypatch.setenv("MCP_BRIDGE_MODELS_CATALOG", "/nonexistent/models.json")

    # No model id -> default context window (128k) * default fraction (0.75).
    assert get_max_context_tokens() == 96000


def test_get_max_context_tokens_reads_environment(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", "50000")

    assert get_max_context_tokens() == 50000


def test_get_max_context_tokens_clamps_too_low_environment_values(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", "100")

    assert get_max_context_tokens() == 1000


def test_get_max_context_tokens_derives_from_model_context_window(monkeypatch):
    monkeypatch.delenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("MCP_BRIDGE_CONTEXT_BUDGET_FRACTION", raising=False)
    monkeypatch.setenv("MCP_BRIDGE_MODELS_CATALOG", "/nonexistent/models.json")

    # minimax-m3 has a 1M context window -> 1M * 0.75 = 750000.
    assert get_max_context_tokens("minimax/minimax-m3:free") == 750000


def test_get_max_context_tokens_parses_context_hint_from_model_id(monkeypatch):
    monkeypatch.delenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("MCP_BRIDGE_CONTEXT_BUDGET_FRACTION", raising=False)
    monkeypatch.setenv("MCP_BRIDGE_MODELS_CATALOG", "/nonexistent/models.json")

    # "200k" in the id -> 200000 * 0.75 = 150000.
    assert get_max_context_tokens("some-vendor/model-200k:free") == 150000


def test_get_max_context_tokens_respects_budget_fraction_env(monkeypatch):
    monkeypatch.delenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("MCP_BRIDGE_CONTEXT_BUDGET_FRACTION", "0.5")
    monkeypatch.setenv("MCP_BRIDGE_MODELS_CATALOG", "/nonexistent/models.json")

    # minimax-m3 1M * 0.5 = 500000.
    assert get_max_context_tokens("minimax/minimax-m3:free") == 500000


def test_get_max_context_tokens_reads_from_models_catalog(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("MCP_BRIDGE_CONTEXT_BUDGET_FRACTION", raising=False)
    catalog = tmp_path / "models.json"
    catalog.write_text(
        '{"models": {"acme/model-1:free": {"context_length": 262144}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_BRIDGE_MODELS_CATALOG", str(catalog))

    # Catalog value wins over the known-model/heuristic fallback.
    assert get_max_context_tokens("acme/model-1:free") == 196608


def test_context_budget_exceeded_when_prompt_tokens_over_budget():
    response = CreateChatCompletionResponse.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [],
            "usage": {"prompt_tokens": 70000, "completion_tokens": 10, "total_tokens": 70010},
        }
    )

    assert _context_budget_exceeded(response, 60000) is True


def test_context_budget_not_exceeded_when_under_budget():
    response = CreateChatCompletionResponse.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [],
            "usage": {"prompt_tokens": 30000, "completion_tokens": 10, "total_tokens": 30010},
        }
    )

    assert _context_budget_exceeded(response, 60000) is False


def test_context_budget_not_exceeded_when_usage_missing():
    response = CreateChatCompletionResponse.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [],
        }
    )

    assert _context_budget_exceeded(response, 60000) is False


def test_tool_loop_uses_partial_evidence_when_tool_call_times_out():
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
        ["fetch_content: Timeout Error calling fetch_content"],
        request_messages,
    ) is False


def test_record_timing_emits_elapsed_ms():
    trace_logger = DummyTraceLogger()
    _record_timing(trace_logger, "tool_dispatch", time.perf_counter() - 0.01)

    assert trace_logger.events[0]["type"] == "timing"
    assert trace_logger.events[0]["stage"] == "tool_dispatch"
    assert trace_logger.events[0]["elapsed_ms"] >= 0


def test_has_only_weak_tool_evidence_detects_empty_search_fallbacks():
    request_messages = [
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "Google blocked by bot detection for this request. Showing fallback web results."}],
                "tool_call_id": "call_1",
            }
        ),
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "No results were found for your search query. Please try rephrasing your search."}],
                "tool_call_id": "call_2",
            }
        ),
    ]

    assert _has_only_weak_tool_evidence(request_messages) is True


def test_has_only_weak_tool_evidence_allows_useful_search_results():
    request_messages = [
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "Repository: example/repo includes an MCP server implementation"}],
                "tool_call_id": "call_1",
            }
        )
    ]

    assert _has_only_weak_tool_evidence(request_messages) is False


def test_format_tool_loop_stop_message_includes_turns_and_limit():
    message = _format_tool_loop_stop_message(tool_turns_completed=3, max_tool_turns=12)

    assert message == "stopping tool loop after 3 turn(s); max_tool_turns=12"


def test_detect_repeated_tool_calls_flags_third_identical_call():
    seen: dict[str, int] = {}
    # First iteration: not repeated.
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas ceremony"}')], seen) is False
    # Second iteration with the same call: still not repeated (threshold is 3).
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas ceremony"}')], seen) is False
    # Third iteration with the same call: repeated.
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas ceremony"}')], seen) is True


def test_detect_repeated_tool_calls_ignores_whitespace_differences():
    seen: dict[str, int] = {}
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas   ceremony"}')], seen) is False
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas ceremony"}')], seen) is False
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas ceremony"}')], seen) is True


def test_detect_repeated_tool_calls_does_not_flag_distinct_queries():
    seen: dict[str, int] = {}
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas"}')], seen) is False
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Sang e Buniyaad"}')], seen) is False


def test_should_use_empty_content_fallback_only_when_there_are_no_tool_calls():
    empty_message = ChatCompletionRequestMessage.model_validate({"role": "assistant", "content": ""})
    assert _should_use_empty_content_fallback(empty_message, "stop") is True

    tool_call_message = ChatCompletionRequestMessage.model_validate(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_test",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        }
    )
    assert _should_use_empty_content_fallback(tool_call_message, "tool_calls") is False


def test_extract_message_text_handles_mapping_messages():
    message = {"content": [{"type": "text", "text": "hello from a mapping"}]}

    assert _extract_message_text(message) == "hello from a mapping"


def test_extract_tool_message_text_handles_mapping_tool_messages():
    message = {"role": "tool", "content": [{"type": "text", "text": "useful evidence from a mapping"}]}

    assert _extract_tool_message_text(message) == "useful evidence from a mapping"


def test_build_empty_content_response_uses_tool_evidence_when_available():
    response = CreateChatCompletionResponse.model_validate(
        {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                    },
                    "finish_reason": "stop",
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

    fallback_response = _build_empty_content_response(
        response,
        request_messages=request_messages,
        stop_reason="empty_response",
    )

    content = fallback_response.choices[0].message.content or ""
    assert "partial search results" in content
    assert fallback_response.choices[0].message.tool_calls is None
    assert fallback_response.choices[0].finish_reason == FinishReason1.stop


def test_build_synthesis_request_adds_instruction_and_drops_tools():
    request = CreateChatCompletionRequest.model_validate(
        {
            "messages": [
                {"role": "system", "content": "You are helpful."},
            ],
            "model": "test-model",
        }
    )
    synthesis_request = _build_synthesis_request(
        request,
        stop_reason="max_tool_turns",
        request_messages=[ChatCompletionRequestMessage.model_validate({"role": "tool", "content": [{"type": "text", "text": "useful evidence"}], "tool_call_id": "call_1"})],
    )

    assert synthesis_request.tools == []
    last_message = synthesis_request.messages[-1]
    last_role = getattr(getattr(last_message, "root", last_message), "role", None)
    last_role_value = getattr(last_role, "value", last_role)
    assert last_role_value == "user"
    last_content = getattr(getattr(last_message, "root", last_message), "content", None)
    assert "synthesiz" in str(last_content).lower()


def test_build_synthesis_request_force_answer_uses_direct_instruction():
    request = CreateChatCompletionRequest.model_validate(
        {
            "messages": [
                {"role": "system", "content": "You are helpful."},
            ],
            "model": "test-model",
        }
    )
    synthesis_request = _build_synthesis_request(
        request,
        stop_reason="repeated_tool_calls",
        request_messages=[ChatCompletionRequestMessage.model_validate({"role": "tool", "content": [{"type": "text", "text": "useful evidence"}], "tool_call_id": "call_1"})],
        force_answer=True,
    )

    assert synthesis_request.tools == []
    last_message = synthesis_request.messages[-1]
    last_content = getattr(getattr(last_message, "root", last_message), "content", None)
    assert "do not call any tools" in str(last_content).lower()


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


def test_build_tool_loop_stop_response_prefers_earlier_informative_tool_outputs():
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
                "content": [{"type": "text", "text": "Earlier result: a useful search result was found"}],
                "tool_call_id": "call_1",
            }
        ),
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "No results were found for your search query. This could be due to DuckDuckGo's bot detection or the query returned no matches. Please try rephrasing your search or try again in a few minutes."}],
                "tool_call_id": "call_2",
            }
        ),
    ]

    stop_response = _build_tool_loop_stop_response(
        response,
        stop_reason="max_tool_turns",
        request_messages=request_messages,
    )

    content = stop_response.choices[0].message.content or ""
    assert "Earlier result: a useful search result was found" in content
    assert "No results were found" not in content


def test_build_tool_loop_stop_response_combines_multiple_informative_tool_outputs():
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
                "content": [{"type": "text", "text": "Earlier result: a useful search result was found"}],
                "tool_call_id": "call_1",
            }
        ),
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "Another useful result: the source mentions a relevant fact"}],
                "tool_call_id": "call_2",
            }
        ),
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "No results were found for your search query. This could be due to DuckDuckGo's bot detection or the query returned no matches. Please try rephrasing your search or try again in a few minutes."}],
                "tool_call_id": "call_3",
            }
        ),
    ]

    stop_response = _build_tool_loop_stop_response(
        response,
        stop_reason="max_tool_turns",
        request_messages=request_messages,
    )

    content = stop_response.choices[0].message.content or ""
    assert "Earlier result: a useful search result was found" in content
    assert "Another useful result: the source mentions a relevant fact" in content
    assert "No results were found" not in content
    assert "I found some search results" in content


def test_build_tool_loop_stop_response_offers_helpful_guidance_when_evidence_is_weak():
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
                "content": [{"type": "text", "text": "No results were found for your search query. Please try rephrasing your search or try again in a few minutes."}],
                "tool_call_id": "call_1",
            }
        )
    ]

    stop_response = _build_tool_loop_stop_response(
        response,
        stop_reason="max_tool_turns",
        request_messages=request_messages,
    )

    content = stop_response.choices[0].message.content or ""
    assert "I wasn't able to gather enough reliable evidence" in content
    assert "narrower or more specific query" in content


def test_build_tool_loop_stop_response_summarizes_tool_evidence_in_plain_language():
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
                "content": [{"type": "text", "text": "The repository includes an MCP server implementation"}],
                "tool_call_id": "call_1",
            }
        ),
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "The project is open source and focused on AI coding assistants"}],
                "tool_call_id": "call_2",
            }
        ),
    ]

    stop_response = _build_tool_loop_stop_response(
        response,
        stop_reason="max_tool_turns",
        request_messages=request_messages,
    )

    content = stop_response.choices[0].message.content or ""
    assert "The repository includes an MCP server implementation" in content
    assert "The project is open source and focused on AI coding assistants" in content
    assert " | " not in content


def test_build_tool_loop_stop_response_uses_search_context_when_tool_calls_are_search_like():
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
                                "function": {"name": "searchGitHub", "arguments": '{"query": "test"}'},
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
                "content": [{"type": "text", "text": "Repository: example/repo with an MCP server implementation"}],
                "tool_call_id": "call_1",
            }
        )
    ]

    stop_response = _build_tool_loop_stop_response(
        response,
        stop_reason="max_tool_turns",
        request_messages=request_messages,
    )

    content = stop_response.choices[0].message.content or ""
    assert "search results" in content.lower()
    assert "Repository: example/repo" in content


def test_build_tool_loop_stop_response_strips_boilerplate_prefixes_from_tool_messages():
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
                                "function": {"name": "searchGitHub", "arguments": '{"query": "test"}'},
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
                "content": [{"type": "text", "text": "Result: The repository includes an MCP server implementation"}],
                "tool_call_id": "call_1",
            }
        )
    ]

    stop_response = _build_tool_loop_stop_response(
        response,
        stop_reason="max_tool_turns",
        request_messages=request_messages,
    )

    content = stop_response.choices[0].message.content or ""
    assert "Result:" not in content
    assert "The repository includes an MCP server implementation" in content


def test_parse_pseudo_tool_calls_extracts_single_invoke():
    text = (
        "<dots_function_call>\n"
        '<invoke name="fetch">\n'
        '<parameter name="url">\n'
        "https://abdurrahman.org/2014/01/29/evilsofnationalism/\n"
        "</parameter>\n"
        "</invoke>\n"
        "</dots_function_call>"
    )

    calls = _parse_pseudo_tool_calls(text)

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "fetch"
    assert '"url"' in arguments
    assert "https://abdurrahman.org/2014/01/29/evilsofnationalism/" in arguments


def test_parse_pseudo_tool_calls_handles_multiple_invokes():
    text = (
        '<invoke name="search"><parameter name="query">hello</parameter></invoke>\n'
        '<invoke name="fetch"><parameter name="url">https://example.com</parameter></invoke>'
    )

    calls = _parse_pseudo_tool_calls(text)

    assert len(calls) == 2
    assert calls[0][0] == "search"
    assert calls[1][0] == "fetch"


def test_parse_pseudo_tool_calls_coerces_json_values():
    text = (
        '<invoke name="search">'
        '<parameter name="query">test</parameter>'
        '<parameter name="max_results">5</parameter>'
        '<parameter name="enabled">true</parameter>'
        "</invoke>"
    )

    calls = _parse_pseudo_tool_calls(text)

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "search"
    parsed = __import__("json").loads(arguments)
    assert parsed["query"] == "test"
    assert parsed["max_results"] == 5
    assert parsed["enabled"] is True


def test_parse_pseudo_tool_calls_returns_empty_for_no_markers():
    assert _parse_pseudo_tool_calls("just some text") == []
    assert _parse_pseudo_tool_calls("") == []
    assert _parse_pseudo_tool_calls(None) == []


def test_parse_pseudo_tool_calls_handles_dots_studio_multi_tool_format():
    # Exact format emitted by dots-studio/dots-3-note-preview:free
    text = (
        "<dots_function_call>\n"
        '<invoke name="fetch">\n'
        '<parameter name="url">\n'
        "https://www.salafitalk.net/st/viewmessages.cfm?Forum=8&Topic=9887\n"
        "</parameter>\n"
        "</invoke>\n"
        "</dots_function_call>\n"
        "<dots_function_call>\n"
        '<invoke name="fetch">\n'
        '<parameter name="url">\n'
        "https://abdurrahman.org/category/islam/bidah-innovated-celebrations/page/2/\n"
        "</parameter>\n"
        "</invoke>\n"
        "</dots_function_call>\n"
        "<dots_function_call>\n"
        '<invoke name="google_search">\n'
        '<parameter name="query">\n'
        "Permanent Committee for Fatwa Saudi Arabia national day bidah fatwa volume 3 pages 86-89\n"
        "</parameter>\n"
        '<parameter name="num_results">\n'
        "10\n"
        "</parameter>\n"
        "</invoke>\n"
        "</dots_function_call>"
    )

    calls = _parse_pseudo_tool_calls(text)

    assert len(calls) == 3
    assert calls[0][0] == "fetch"
    assert "salafitalk.net" in calls[0][1]
    assert calls[1][0] == "fetch"
    assert "abdurrahman.org" in calls[1][1]
    assert calls[2][0] == "google_search"
    parsed = __import__("json").loads(calls[2][1])
    assert parsed["query"] == "Permanent Committee for Fatwa Saudi Arabia national day bidah fatwa volume 3 pages 86-89"
    assert parsed["num_results"] == 10


def test_build_synthetic_tool_calls_creates_expected_shape():
    calls = _build_synthetic_tool_calls([("fetch", '{"url": "https://example.com"}')])

    assert len(calls) == 1
    tool_call = calls[0]
    # Provider-compatible OpenAI-style ID (minimax rejects "pseudo-call-0").
    assert tool_call.id.startswith("call_")
    assert tool_call.type == "function"
    assert tool_call.function.name == "fetch"
    assert tool_call.function.arguments == '{"url": "https://example.com"}'


def test_strip_pseudo_tool_call_markers_removes_tool_call_blocks():
    # nemotron-3-super-120b format: <tool_call><function=NAME>...</function></tool_call>
    text = (
        "Let me search.\n"
        "<tool_call>\n"
        "<function=google_search>\n"
        "<parameter=query>\n"
        "some query\n"
        "</parameter>\n"
        "</function>\n"
        "</tool_call>\n"
    )

    stripped = _strip_pseudo_tool_call_markers(text)

    assert "<tool_call>" not in stripped
    assert "<function=" not in stripped
    assert "<parameter=" not in stripped
    assert "Let me search." in stripped


def test_strip_pseudo_tool_call_markers_removes_invoke_blocks():
    text = (
        '<invoke name="search"><parameter name="query">hello</parameter></invoke>\n'
        "Some remaining text"
    )

    stripped = _strip_pseudo_tool_call_markers(text)

    assert "<invoke" not in stripped
    assert "<parameter" not in stripped
    assert "Some remaining text" in stripped


def test_strip_pseudo_tool_call_markers_removes_dots_function_call_wrappers():
    text = (
        "<dots_function_call>\n"
        '<invoke name="fetch"><parameter name="url">https://example.com</parameter></invoke>\n'
        "</dots_function_call>\n"
    )

    stripped = _strip_pseudo_tool_call_markers(text)

    assert "<dots_function_call>" not in stripped
    assert "<invoke" not in stripped
    assert stripped == ""


def test_parse_pseudo_tool_calls_handles_function_equals_format():
    # Exact format emitted by nvidia/nemotron-3-super-120b-a12b:free
    text = (
        "<tool_call>\n"
        "<function=fetch>\n"
        "<parameter=max_length>\n"
        "3000\n"
        "</parameter>\n"
        "<parameter=url>\n"
        "https://www.alqayim.net/ar/artical/17/d-805\n"
        "</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )

    calls = _parse_pseudo_tool_calls(text)

    assert len(calls) == 1
    name, arguments = calls[0]
    assert name == "fetch"
    parsed = __import__("json").loads(arguments)
    assert parsed["max_length"] == 3000
    assert parsed["url"] == "https://www.alqayim.net/ar/artical/17/d-805"


def test_parse_pseudo_tool_calls_handles_multiple_function_equals_blocks():
    text = (
        "<tool_call><function=search><parameter=query>hello</parameter></function></tool_call>\n"
        "<tool_call><function=fetch><parameter=url>https://example.com</parameter></function></tool_call>"
    )

    calls = _parse_pseudo_tool_calls(text)

    assert len(calls) == 2
    assert calls[0][0] == "search"
    assert calls[1][0] == "fetch"


def test_context_budget_nearly_exceeded_detects_approaching_limit():
    response = CreateChatCompletionResponse.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [],
            "usage": {"prompt_tokens": 50000, "completion_tokens": 10, "total_tokens": 50010},
        }
    )

    assert _context_budget_nearly_exceeded(response, 60000) is True


def test_context_budget_nearly_exceeded_false_when_well_under():
    response = CreateChatCompletionResponse.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [],
            "usage": {"prompt_tokens": 10000, "completion_tokens": 10, "total_tokens": 10010},
        }
    )

    assert _context_budget_nearly_exceeded(response, 60000) is False


def _assistant_tool_call_message(call_ids: list[str]) -> ChatCompletionRequestMessage:
    return ChatCompletionRequestMessage.model_validate(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "search", "arguments": "{}"}}
                for call_id in call_ids
            ],
        }
    )


def _tool_result_message(call_id: str, text: str) -> ChatCompletionRequestMessage:
    return ChatCompletionRequestMessage.model_validate(
        {
            "role": "tool",
            "content": [{"type": "text", "text": text}],
            "tool_call_id": call_id,
        }
    )


def _assert_no_orphaned_tool_calls_message(messages: list[ChatCompletionRequestMessage]) -> None:
    """Every assistant `tool_calls` message must have ALL of its ids answered
    immediately afterward. This is the actual OpenAI protocol invariant that
    strict providers (observed with Minimax) enforce -- not merely "the id
    exists somewhere in the conversation", but "every call in THIS message is
    answered right here, in full, by the very next messages".
    """
    grouped_assistant_indices = {assistant_index for assistant_index, _ in _group_tool_rounds(messages)}
    for index, message in enumerate(messages):
        inner = getattr(message, "root", message)
        if getattr(inner, "role", None) == "assistant" and _extract_tool_calls(inner):
            assert index in grouped_assistant_indices, (
                f"assistant tool_calls message at index {index} has no matching group of "
                "tool replies immediately after it -- this is exactly the shape strict "
                "providers reject as 'tool call result does not follow tool call'"
            )


def test_compress_tool_context_reduces_message_count():
    # Build a realistic conversation: each round is one assistant `tool_calls`
    # message followed immediately by its own matching `tool` reply.
    messages = [
        ChatCompletionRequestMessage.model_validate({"role": "system", "content": "system"}),
    ]
    for index in range(10):
        messages.append(_assistant_tool_call_message([f"call_{index}"]))
        messages.append(_tool_result_message(f"call_{index}", f"Search result {index} with some useful evidence"))

    compressed = _compress_tool_context(messages, keep_recent_rounds=3)

    assert compressed is True
    # 1 system + 1 summary + 3 kept rounds (assistant + tool, each) = 8.
    # The 7 oldest ROUNDS (assistant message AND its tool reply together) are
    # collapsed into the single summary -- unlike the old per-message
    # compression, the assistant messages of compressed rounds are removed
    # too, since leaving them behind with no matching reply is exactly the
    # bug this function now guards against.
    assert len(messages) == 8
    summary_texts = [
        _extract_message_text(m) or ""
        for m in messages
        if getattr(getattr(m, "root", m), "role", None) == "user"
    ]
    assert any("summarized" in text for text in summary_texts)

    _assert_no_orphaned_tool_calls_message(messages)


def test_compress_tool_context_never_splits_a_multi_call_round():
    """Regression test for the actual reported failure: a round with several
    tool calls must be compressed or kept as one atomic unit. The old
    implementation cut by raw tool-MESSAGE count, so a round with multiple
    calls could straddle the cut -- some of its replies kept, some
    compressed away, while its assistant `tool_calls` message (which lists
    ALL the ids) was always kept. That leaves an assistant message
    referencing call ids whose replies no longer immediately follow it,
    which Minimax's backend rejects with "invalid params ... tool call
    result does not follow tool call (2013)".

    Round sizes here are [1, 1, 3, 1, 1] (7 tool messages total). Keeping the
    last 3 tool MESSAGES (the old behavior) would land exactly inside the
    3-call round, keeping only its last reply. Keeping the last 2 ROUNDS
    (this test) must keep or drop round 3 -- with 3 calls -- as a whole.
    """
    messages = [ChatCompletionRequestMessage.model_validate({"role": "system", "content": "system"})]
    round_call_ids = [["call_1"], ["call_2"], ["call_3a", "call_3b", "call_3c"], ["call_4"], ["call_5"]]
    for call_ids in round_call_ids:
        messages.append(_assistant_tool_call_message(call_ids))
        for call_id in call_ids:
            messages.append(_tool_result_message(call_id, f"result for {call_id}"))

    compressed = _compress_tool_context(messages, keep_recent_rounds=2)

    assert compressed is True
    _assert_no_orphaned_tool_calls_message(messages)

    # The two most recent rounds (call_4, call_5) must survive untouched.
    remaining_tool_call_ids = {
        _message_tool_call_id_for_test(m) for m in messages if getattr(getattr(m, "root", m), "role", None) == "tool"
    }
    assert remaining_tool_call_ids == {"call_4", "call_5"}


def _message_tool_call_id_for_test(message: ChatCompletionRequestMessage) -> str | None:
    inner = getattr(message, "root", message)
    return getattr(inner, "tool_call_id", None)


def test_compress_tool_context_returns_false_when_few_rounds():
    messages = [
        ChatCompletionRequestMessage.model_validate({"role": "system", "content": "system"}),
        _assistant_tool_call_message(["call_1"]),
        _tool_result_message("call_1", "Search result 1"),
        _assistant_tool_call_message(["call_2"]),
        _tool_result_message("call_2", "Search result 2"),
    ]

    # Only 2 whole rounds exist; keep_recent_rounds=3 means there's nothing
    # old enough to compress.
    compressed = _compress_tool_context(messages, keep_recent_rounds=3)

    assert compressed is False
    assert len(messages) == 5


def _fake_response_with_content(content: str | None) -> CreateChatCompletionResponse:
    return CreateChatCompletionResponse.model_validate(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content or ""},
                }
            ],
        }
    )


def test_record_and_raise_upstream_failure_logs_full_detail_and_points_to_trace():
    """A hard failure must be traceable in full: the trace file gets the FULL
    raw upstream body under a well-known event type, and the client-visible
    detail (what test.sh prints via `cat response.json`) points at the exact
    trace file to open for the rest of the story."""
    trace_logger = DummyTraceLogger()
    raw_body = '{"error": {"message": "some very long provider-specific error body"}}'

    exc = _record_and_raise_upstream_failure(
        trace_logger,
        status_code=502,
        stage="upstream_error_status",
        detail="Upstream inference server returned status 400: invalid params (2013)",
        model="minimax/minimax-m2.7:free",
        raw_body=raw_body,
    )

    assert exc.status_code == 502
    # The client-visible detail names the trace file so a human can go
    # straight from terminal output to the full story.
    assert trace_logger.path in exc.detail
    assert "invalid params (2013)" in exc.detail

    error_events = [e for e in trace_logger.events if e["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["status_code"] == 502
    assert error_events[0]["stage"] == "upstream_error_status"
    assert error_events[0]["model"] == "minimax/minimax-m2.7:free"
    # The trace gets the FULL raw body, not a truncated client-facing snippet.
    assert error_events[0]["raw_body"] == raw_body


def test_record_and_raise_upstream_failure_without_trace_logger():
    # Must not crash when no trace logger is active (e.g. streaming path).
    exc = _record_and_raise_upstream_failure(
        None,
        status_code=502,
        stage="no_choices",
        detail="Upstream chat completion response contained no choices",
    )
    assert exc.status_code == 502
    assert "trace log" not in exc.detail


def test_degraded_marker_comment_is_html_comment_with_reason_and_trace():
    comment = _degraded_marker_comment("empty_response", "logs/20260101T000000Z_x.json")
    assert comment.startswith("<!--")
    assert comment.endswith("-->")
    assert 'reason="empty_response"' in comment
    assert 'trace="logs/20260101T000000Z_x.json"' in comment


def test_finalize_degraded_response_marks_content_and_records_event():
    """A synthesized fallback answer (HTTP 200) must be distinguishable from a
    genuine model answer: an invisible marker in the content (for pipelines
    like test.sh to grep) and a trace event (for deep debugging)."""
    trace_logger = DummyTraceLogger()
    response = _fake_response_with_content("Note: the workflow stopped early.")

    result = _finalize_degraded_response(response, trace_logger=trace_logger, reason="max_tool_turns")

    assert result.choices[0].message.content.startswith("<!-- mcp-bridge:degraded")
    assert "Note: the workflow stopped early." in result.choices[0].message.content

    degraded_events = [e for e in trace_logger.events if e["type"] == "degraded_response"]
    assert len(degraded_events) == 1
    assert degraded_events[0]["reason"] == "max_tool_turns"


def test_finalize_recovered_response_does_not_touch_content():
    """When the tool loop stopped early but the model still produced a real
    answer via synthesis, the content must be left exactly as the model wrote
    it -- only a trace event records that the run took an unusual path."""
    trace_logger = DummyTraceLogger()
    response = _fake_response_with_content("A complete, genuine answer from the model.")

    result = _finalize_recovered_response(response, trace_logger=trace_logger, reason="repeated_tool_calls")

    assert result.choices[0].message.content == "A complete, genuine answer from the model."

    recovered_events = [e for e in trace_logger.events if e["type"] == "early_stop_recovered"]
    assert len(recovered_events) == 1
    assert recovered_events[0]["reason"] == "repeated_tool_calls"
