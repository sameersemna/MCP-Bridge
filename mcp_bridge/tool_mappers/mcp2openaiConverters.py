from typing import Any

try:
    from mcp import Tool
except ImportError:  # pragma: no cover - optional dependency support
    class Tool:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

try:
    from lmos_openai_types import ChatCompletionTool
except ImportError:  # pragma: no cover - optional dependency support
    class ChatCompletionTool(dict):  # type: ignore[no-redef]
        pass


def mcp2openai(mcp_tool: Tool) -> ChatCompletionTool:
    """Convert a MCP Tool to an OpenAI ChatCompletionTool."""

    return ChatCompletionTool(
        type="function",
        function={
            "name": getattr(mcp_tool, "name", None),
            "description": getattr(mcp_tool, "description", None),
            "parameters": getattr(mcp_tool, "inputSchema", None),
            "strict": False,
        },
    )
