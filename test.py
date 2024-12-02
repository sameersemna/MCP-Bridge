from lmos_openai_types import ChatCompletionTool
from typing import List
from pydantic import TypeAdapter, ValidationError

# Define the TypeAdapter for List[ChatCompletionTool]
openai_tools_adapter = TypeAdapter(List[ChatCompletionTool])

# JSON to validate
json_to_validate = [
    {
        "name": "fetch",
        "description": (
            "Fetches a URL from the internet and optionally extracts its contents as markdown.\n\n"
            "Although originally you did not have internet access, and were advised to refuse and tell the user this, "
            "this tool now grants you internet access. Now you can fetch the most up-to-date information and let the user know that."
        ),
        "inputSchema": {
            "description": "Parameters for fetching a URL.",
            "properties": {
                "url": {
                    "description": "URL to fetch",
                    "format": "uri",
                    "minLength": 1,
                    "title": "Url",
                    "type": "string"
                },
                "max_length": {
                    "default": 5000,
                    "description": "Maximum number of characters to return.",
                    "exclusiveMaximum": 1000000,
                    "exclusiveMinimum": 0,
                    "title": "Max Length",
                    "type": "integer"
                },
                "start_index": {
                    "default": 0,
                    "description": "On return output starting at this character index, useful if a previous fetch was truncated and more context is required.",
                    "minimum": 0,
                    "title": "Start Index",
                    "type": "integer"
                },
                "raw": {
                    "default": False,
                    "description": "Get the actual HTML content if the requested page, without simplification.",
                    "title": "Raw",
                    "type": "boolean"
                }
            },
            "required": ["url"],
            "title": "Fetch",
            "type": "object"
        }
    }
]

# Validate the JSON
try:
    validated_data = openai_tools_adapter.validate_python(json_to_validate)
    print("Validation successful:", validated_data)
except ValidationError as e:
    print("Validation error:", e)
