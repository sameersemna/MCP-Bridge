from mcp_bridge.openai_clients.utils import (
    _TOOL_SCHEMA_CACHE,
    _cache_tool_schema,
    _parse_lenient_json,
    repair_tool_arguments,
)


def _clear_cache():
    _TOOL_SCHEMA_CACHE.clear()


def test_repair_fills_missing_required_boolean_for_sequential_thinking():
    _clear_cache()
    _cache_tool_schema(
        "sequentialthinking",
        {
            "type": "object",
            "required": ["thought", "nextThoughtNeeded", "thoughtNumber", "totalThoughts"],
            "properties": {
                "thought": {"type": "string"},
                "nextThoughtNeeded": {"type": "boolean"},
                "thoughtNumber": {"type": "integer"},
                "totalThoughts": {"type": "integer"},
            },
        },
    )

    # The exact failure captured in production logs: nextThoughtNeeded omitted.
    args = {"thought": "analyze the problem", "thoughtNumber": 1, "totalThoughts": 8}

    repaired = repair_tool_arguments("sequentialthinking", args)

    assert repaired["nextThoughtNeeded"] is True
    assert repaired["thought"] == "analyze the problem"
    assert repaired["thoughtNumber"] == 1
    assert repaired["totalThoughts"] == 8


def test_repair_coerces_string_boolean_to_real_boolean():
    _clear_cache()
    _cache_tool_schema(
        "sequentialthinking",
        {
            "type": "object",
            "required": ["thought", "nextThoughtNeeded", "thoughtNumber", "totalThoughts"],
            "properties": {
                "thought": {"type": "string"},
                "nextThoughtNeeded": {"type": "boolean"},
                "thoughtNumber": {"type": "integer"},
                "totalThoughts": {"type": "integer"},
            },
        },
    )

    args = {
        "thought": "step",
        "nextThoughtNeeded": "false",
        "thoughtNumber": "2",
        "totalThoughts": "5",
    }

    repaired = repair_tool_arguments("sequentialthinking", args)

    assert repaired["nextThoughtNeeded"] is False
    assert repaired["thoughtNumber"] == 2
    assert repaired["totalThoughts"] == 5


def test_repair_drops_unknown_keys_when_additional_properties_false():
    _clear_cache()
    _cache_tool_schema(
        "strict_tool",
        {
            "type": "object",
            "required": ["url"],
            "additionalProperties": False,
            "properties": {"url": {"type": "string"}},
        },
    )

    args = {"url": "https://example.com", "bogus_extra": 123}

    repaired = repair_tool_arguments("strict_tool", args)

    assert repaired == {"url": "https://example.com"}


def test_repair_keeps_unknown_keys_when_additional_properties_allowed():
    _clear_cache()
    _cache_tool_schema(
        "loose_tool",
        {
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string"}},
        },
    )

    args = {"query": "hello", "extra": "kept"}

    repaired = repair_tool_arguments("loose_tool", args)

    assert repaired == {"query": "hello", "extra": "kept"}


def test_repair_uses_schema_default_for_missing_required_field():
    _clear_cache()
    _cache_tool_schema(
        "fetch",
        {
            "type": "object",
            "required": ["url", "max_length"],
            "properties": {
                "url": {"type": "string"},
                "max_length": {"type": "integer", "default": 5000},
            },
        },
    )

    args = {"url": "https://example.com"}

    repaired = repair_tool_arguments("fetch", args)

    assert repaired["url"] == "https://example.com"
    assert repaired["max_length"] == 5000


def test_repair_returns_arguments_unchanged_without_schema():
    _clear_cache()

    args = {"thought": "x", "thoughtNumber": 1}

    assert repair_tool_arguments("sequentialthinking", args) == args


def test_repair_handles_non_dict_arguments():
    _clear_cache()

    assert repair_tool_arguments("sequentialthinking", "not-a-dict") == "not-a-dict"
    assert repair_tool_arguments("sequentialthinking", None) is None


def test_repair_resolves_anyof_branch():
    _clear_cache()
    _cache_tool_schema(
        "flex_tool",
        {
            "type": "object",
            "required": ["value"],
            "properties": {
                "value": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ]
                }
            },
        },
    )

    assert repair_tool_arguments("flex_tool", {"value": "42"})["value"] == "42"
    assert repair_tool_arguments("flex_tool", {"value": 42})["value"] == 42


def test_repair_coerces_nested_array_items():
    _clear_cache()
    _cache_tool_schema(
        "list_tool",
        {
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {"type": "array", "items": {"type": "integer"}},
            },
        },
    )

    repaired = repair_tool_arguments("list_tool", {"items": ["1", "2", "3"]})

    assert repaired["items"] == [1, 2, 3]


def test_parse_lenient_json_handles_trailing_commas():
    assert _parse_lenient_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_parse_lenient_json_handles_single_quotes():
    assert _parse_lenient_json("{'a': 'hello', 'b': 2}") == {"a": "hello", "b": 2}


def test_parse_lenient_json_handles_unquoted_keys():
    assert _parse_lenient_json("{a: 1, b: true}") == {"a": 1, "b": True}


def test_parse_lenient_json_handles_code_fence():
    assert _parse_lenient_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_lenient_json_returns_none_for_garbage():
    assert _parse_lenient_json("not json at all") is None
    assert _parse_lenient_json("") is None
