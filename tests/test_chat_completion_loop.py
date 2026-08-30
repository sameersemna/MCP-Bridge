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
    _detect_repeated_tool_calls,
    _extract_message_text,
    _extract_tool_message_text,
    _format_tool_loop_stop_message,
    _has_only_weak_tool_evidence,
    _parse_pseudo_tool_calls,
    _record_timing,
    _should_stop_tool_loop_on_tool_errors,
    _should_use_empty_content_fallback,
    get_max_context_tokens,
    get_max_tool_turns,
    should_continue_tool_loop,
)


class DummyTraceLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

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

    assert get_max_context_tokens() == 60000


def test_get_max_context_tokens_reads_environment(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", "50000")

    assert get_max_context_tokens() == 50000


def test_get_max_context_tokens_clamps_too_low_environment_values(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS", "100")

    assert get_max_context_tokens() == 1000


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


def test_detect_repeated_tool_calls_flags_second_identical_call():
    seen: dict[str, int] = {}
    # First iteration: not repeated.
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas ceremony"}')], seen) is False
    # Second iteration with the same call: repeated.
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas ceremony"}')], seen) is True


def test_detect_repeated_tool_calls_ignores_whitespace_differences():
    seen: dict[str, int] = {}
    assert _detect_repeated_tool_calls([("google_search", '{"query": "Hajr al-Asas   ceremony"}')], seen) is False
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
    assert tool_call.id == "pseudo-call-0"
    assert tool_call.type == "function"
    assert tool_call.function.name == "fetch"
    assert tool_call.function.arguments == '{"url": "https://example.com"}'


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


def test_compress_tool_context_reduces_message_count():
    messages = [
        ChatCompletionRequestMessage.model_validate({"role": "system", "content": "system"}),
    ]
    for index in range(10):
        messages.append(
            ChatCompletionRequestMessage.model_validate(
                {
                    "role": "tool",
                    "content": [{"type": "text", "text": f"Search result {index} with some useful evidence"}],
                    "tool_call_id": f"call_{index}",
                }
            )
        )

    compressed = _compress_tool_context(messages, keep_recent=3)

    assert compressed is True
    # 1 system + 1 summary + 3 recent tool messages = 5
    assert len(messages) == 5
    # The summary message should be present.
    summary_texts = [
        _extract_tool_message_text(m) or ""
        for m in messages
        if getattr(getattr(m, "root", m), "role", None) == "tool"
    ]
    assert any("summarized" in text for text in summary_texts)


def test_compress_tool_context_returns_false_when_few_messages():
    messages = [
        ChatCompletionRequestMessage.model_validate({"role": "system", "content": "system"}),
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "Search result 1"}],
                "tool_call_id": "call_1",
            }
        ),
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "tool",
                "content": [{"type": "text", "text": "Search result 2"}],
                "tool_call_id": "call_2",
            }
        ),
    ]

    compressed = _compress_tool_context(messages, keep_recent=3)

    assert compressed is False
    assert len(messages) == 3
