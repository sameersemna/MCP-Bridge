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
