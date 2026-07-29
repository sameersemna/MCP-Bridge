try:
    import mcp.shared.exceptions as exceptions
except Exception:  # pragma: no cover
    exceptions = None

if exceptions is not None:
    try:
        from mcp.shared.exceptions import McpError as _McpError
    except Exception:  # pragma: no cover
        _McpError = RuntimeError

    if not hasattr(exceptions, "McpError"):
        exceptions.McpError = _McpError
