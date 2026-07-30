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

    tool_name = getattr(mcp_tool, "name", None)
    description = getattr(mcp_tool, "description", None)
    if description and tool_name == "searchGitHub":
        description = (
            "Use this tool to search real-world code examples from GitHub repositories. "
            "It searches literal code patterns, not broad keywords, and is ideal for finding implementation examples."
        )

    return ChatCompletionTool(
        type="function",
        function={
            "name": tool_name,
            "description": description,
            "parameters": getattr(mcp_tool, "inputSchema", None),
            "strict": False,
        },
    )
