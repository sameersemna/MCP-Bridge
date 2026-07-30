from mcp_bridge.openai_clients.chatCompletion import _format_tool_synthesis, _summarize_tool_messages
from mcp_bridge.openai_clients.utils import (
    clamp_search_tool_arguments,
    truncate_search_result_text,
)


def test_clamp_search_tool_arguments_caps_search_result_count():
    args = {"query": "python mcp", "max_results": 10}

    capped = clamp_search_tool_arguments("search", args)

    assert capped["max_results"] == 5


def test_clamp_search_tool_arguments_leaves_non_search_tools_unchanged():
    args = {"url": "https://example.com", "start_index": 100}

    assert clamp_search_tool_arguments("fetch_content", args) == args


def test_truncate_search_result_text_keeps_only_the_first_requested_results():
    original = """Found 3 search results:\n\r1. First result\n\r   URL: https://example.com/1\n\r   Summary: alpha\n\r2. Second result\n\r   URL: https://example.com/2\n\r   Summary: beta\n\r3. Third result\n\r   URL: https://example.com/3\n\r   Summary: gamma\n\r"""

    truncated = truncate_search_result_text("search", original, max_results=2)

    assert "First result" in truncated
    assert "Second result" in truncated
    assert "Third result" not in truncated
    assert "omitted" in truncated.lower()


def test_format_tool_synthesis_returns_markdown_structure():
    rendered = _format_tool_synthesis("First finding; Second finding", "max_tool_turns", ["search results found"])

    assert "**Search results gathered**" in rendered
    assert "- First finding; Second finding" in rendered
    assert "I found some search results" in rendered


def test_summarize_tool_messages_truncates_long_content():
    long_message = "Found 5 search results: " + " ".join([f"Item {index} details" for index in range(80)])

    summary = _summarize_tool_messages([long_message])

    assert len(summary) < len(long_message)
    assert "…" in summary


def test_summarize_tool_messages_extracts_titles_from_inline_search_results():
    message = (
        "Found 5 search results: 1. Royal families of the United Arab Emirates - Wikipedia "
        "URL: https://example.com/uae Summary: The royal families of the United Arab Emirates consist of the six ruling families. "
        "2. UAE dismisses unfounded accusations of involvement in Khartoum - MSN "
        "URL: https://example.com/uae-accusations Summary: The UAE says the allegations are unfounded."
    )

    summary = _summarize_tool_messages([message])

    assert summary.startswith("Top findings:")
    assert "Royal families of the United Arab Emirates" in summary
    assert "UAE dismisses unfounded accusations" in summary
    assert "Summary:" not in summary
