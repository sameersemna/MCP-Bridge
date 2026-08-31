
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


def test_should_log_stderr_text_ignores_benign_pydantic_settings_warning() -> None:
    warning = (
        "IncompleteFieldDefinitionWarning: Field 'lifespan' has an incomplete definition: "
        "its annotation contains an unresolved forward reference, so settings sources may "
        "fail to correctly resolve its value. Call `model_rebuild()` on the model where the "
        "field is defined, once all the referenced types are defined."
    )
    assert _should_log_stderr_text(warning) is False


def test_should_log_stderr_text_ignores_warnings_warn_source_fragment() -> None:
    # The source-code snippet line in a Python warning traceback carries no
    # diagnostic value on its own and should not be surfaced.
    assert _should_log_stderr_text("  warnings.warn(") is False
    assert _should_log_stderr_text("    warnings.warn(") is False


def test_should_log_stderr_text_keeps_real_warnings() -> None:
    assert _should_log_stderr_text("WARNING failed to start server") is True
    assert _should_log_stderr_text("WARNING: connection refused") is True
    # The actual warning message line (with file:line: category) is still kept.
    assert _should_log_stderr_text("file.py:123: UserWarning: some message") is True
