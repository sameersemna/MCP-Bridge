import re

from mcp_bridge.mcp_clients.stdio_transport import _sanitize_stderr_text, _should_log_stderr_text


def test_sanitize_stderr_text_strips_ansi_and_carriage_returns() -> None:
    raw_text = "INFO Processing request of type \x1b[2KCallToolRequest\x1b[0m\rserver.py:733"

    sanitized = _sanitize_stderr_text(raw_text)

    assert sanitized == "INFO Processing request of type CallToolRequest\nserver.py:733"


def test_should_log_stderr_text_ignores_common_mcp_request_trace_lines() -> None:
    assert _should_log_stderr_text("ListToolsRequest") is False
    assert _should_log_stderr_text("Processing request of type CallToolRequest") is False


def test_should_log_stderr_text_keeps_real_errors() -> None:
    assert _should_log_stderr_text("Traceback (most recent call last):") is True
    assert _should_log_stderr_text("WARNING failed to start server") is True


def test_should_log_stderr_text_ignores_common_startup_status_lines() -> None:
    assert _should_log_stderr_text("MCP Server running on stdio") is False
    assert _should_log_stderr_text("Processing request of type") is False
    assert _should_log_stderr_text("Server initialized") is False
