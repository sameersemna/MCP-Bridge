from enum import Enum


class Tag(str, Enum):
    """Tag for OpenAPI"""

    mcp_management = "MCP Management API"
    openai = "OpenAI API Compatible APIs"


tags_metadata = [
    {
        "name": Tag.mcp_management,
        "description": "Interact with and manage the MCP servers",
    },
    {
        "name": Tag.openai,
        "description": "OpenAI compatible endpoints for use with openai clients",
    },
]
