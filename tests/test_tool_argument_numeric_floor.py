"""Regression tests for the below-minimum optional-numeric argument repair.

A model calling ``sequentialthinking`` sent ``revisesThought: 0`` and
``branchFromThought: 0``. Both fields are OPTIONAL with ``minimum: 1``, so the
MCP server rejected the whole call with
``-32602 Input validation error: Too small: expected number to be >=1``. The
bridge then spent an entire extra LLM round trip feeding that error back so the
model could correct ``0`` to ``1``.

Dropping an out-of-range *optional* numeric restores the model's evident intent
(an absent optional field means "not revising / not branching") and lets the
first call succeed, so these tests pin that behaviour -- plus the boundaries
that must NOT change.
"""

from mcp_bridge.openai_clients.utils import (
    _TOOL_SCHEMA_CACHE,
    _below_numeric_floor,
    _cache_tool_schema,
    _numeric_floor,
    repair_tool_arguments,
)

_SEQUENTIAL_SCHEMA = {
    "type": "object",
    "required": ["thought", "nextThoughtNeeded", "thoughtNumber", "totalThoughts"],
    "properties": {
        "thought": {"type": "string"},
        "nextThoughtNeeded": {"type": ["boolean", "string"]},
        "thoughtNumber": {"type": "integer", "minimum": 1},
        "totalThoughts": {"type": "integer", "minimum": 1},
        "revisesThought": {"type": "integer", "minimum": 1},
        "branchFromThought": {"type": "integer", "minimum": 1},
        "branchId": {"type": "string"},
        "isRevision": {"type": ["boolean", "string"]},
    },
}


def _clear_cache() -> None:
    _TOOL_SCHEMA_CACHE.clear()


# --- The regression ---------------------------------------------------------


def test_optional_zero_below_minimum_is_dropped():
    _clear_cache()
    _cache_tool_schema("sequentialthinking", _SEQUENTIAL_SCHEMA)

    # The exact production payload: isRevision/branching are false, but the
    # model still emitted 0 for the two optional numeric fields.
    args = {
        "thought": "Research plan",
        "nextThoughtNeeded": False,
        "thoughtNumber": 1,
        "totalThoughts": 7,
        "isRevision": False,
        "revisesThought": 0,
        "branchFromThought": 0,
        "branchId": "",
    }

    repaired = repair_tool_arguments("sequentialthinking", args)

    assert "revisesThought" not in repaired
    assert "branchFromThought" not in repaired
    # Everything else is preserved untouched.
    assert repaired["thoughtNumber"] == 1
    assert repaired["totalThoughts"] == 7
    assert repaired["nextThoughtNeeded"] is False
    assert repaired["branchId"] == ""


def test_string_zero_is_coerced_then_dropped():
    _clear_cache()
    _cache_tool_schema("sequentialthinking", _SEQUENTIAL_SCHEMA)

    args = {
        "thought": "x",
        "nextThoughtNeeded": True,
        "thoughtNumber": 1,
        "totalThoughts": 3,
        "revisesThought": "0",
    }

    repaired = repair_tool_arguments("sequentialthinking", args)

    assert "revisesThought" not in repaired


def test_negative_below_minimum_is_dropped():
    _clear_cache()
    _cache_tool_schema("sequentialthinking", _SEQUENTIAL_SCHEMA)

    args = {
        "thought": "x",
        "nextThoughtNeeded": True,
        "thoughtNumber": 1,
        "totalThoughts": 3,
        "branchFromThought": -5,
    }

    assert "branchFromThought" not in repair_tool_arguments("sequentialthinking", args)


# --- Boundaries that must NOT change ---------------------------------------


def test_value_at_minimum_is_kept():
    _clear_cache()
    _cache_tool_schema("sequentialthinking", _SEQUENTIAL_SCHEMA)

    args = {
        "thought": "x",
        "nextThoughtNeeded": True,
        "thoughtNumber": 1,
        "totalThoughts": 3,
        "revisesThought": 1,
    }

    assert repair_tool_arguments("sequentialthinking", args)["revisesThought"] == 1


def test_value_above_minimum_is_kept():
    _clear_cache()
    _cache_tool_schema("sequentialthinking", _SEQUENTIAL_SCHEMA)

    args = {
        "thought": "x",
        "nextThoughtNeeded": True,
        "thoughtNumber": 1,
        "totalThoughts": 3,
        "revisesThought": 2,
    }

    assert repair_tool_arguments("sequentialthinking", args)["revisesThought"] == 2


def test_required_below_minimum_is_kept_so_the_server_reports_it():
    _clear_cache()
    _cache_tool_schema(
        "needs_positive",
        {
            "type": "object",
            "required": ["count"],
            "properties": {"count": {"type": "integer", "minimum": 1}},
        },
    )

    # A required field must NOT be silently dropped -- that would trade a
    # "too small" error for a "missing required property" error.
    repaired = repair_tool_arguments("needs_positive", {"count": 0})

    assert repaired == {"count": 0}


def test_boolean_false_is_not_treated_as_zero():
    _clear_cache()
    _cache_tool_schema(
        "flag_tool",
        {
            "type": "object",
            "properties": {"flag": {"type": "boolean"}},
        },
    )

    # bool is an int subclass in Python; False must not be seen as 0.
    repaired = repair_tool_arguments("flag_tool", {"flag": False})

    assert repaired == {"flag": False}
    assert _below_numeric_floor(False, {"type": "boolean"}) is False


def test_value_above_maximum_is_kept():
    _clear_cache()
    _cache_tool_schema(
        "capped",
        {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 10},
            },
        },
    )

    # Over-large is left alone: it is more likely real intent than a placeholder.
    assert repair_tool_arguments("capped", {"n": 99})["n"] == 99


def test_field_without_a_floor_is_untouched():
    _clear_cache()
    _cache_tool_schema(
        "unbounded",
        {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
        },
    )

    assert repair_tool_arguments("unbounded", {"n": 0})["n"] == 0


def test_floor_inside_anyof_branch_is_honored():
    _clear_cache()
    _cache_tool_schema(
        "anyof_tool",
        {
            "type": "object",
            "properties": {
                "n": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]},
            },
        },
    )

    assert "n" not in repair_tool_arguments("anyof_tool", {"n": 0})


# --- Helpers ---------------------------------------------------------------


def test_numeric_floor_reads_minimum_and_exclusive_minimum():
    assert _numeric_floor({"minimum": 1}) == (1.0, False)
    assert _numeric_floor({"exclusiveMinimum": 0}) == (0.0, True)
    assert _numeric_floor({"type": "string"}) is None


def test_exclusive_minimum_boundary_is_exclusive():
    assert _below_numeric_floor(0, {"exclusiveMinimum": 0}) is True
    assert _below_numeric_floor(1, {"exclusiveMinimum": 0}) is False
