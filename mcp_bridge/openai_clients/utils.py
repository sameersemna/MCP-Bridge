import asyncio
import hashlib
import inspect
import json
import os
import re
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from loguru import logger
from opentelemetry import trace

from mcp_bridge.logging import RequestTraceLogger
from mcp_bridge.mcp_clients.AbstractClient import CallToolResult, TextContent

try:
    from lmos_openai_types import ChatCompletionRequestMessage, CreateChatCompletionRequest
except ImportError:  # pragma: no cover - optional dependency support
    class ChatCompletionRequestMessage:  # type: ignore[no-redef]
        def __init__(self, role: str, content: Any = None, **kwargs: Any) -> None:
            self.role = role
            self.content = content
            for key, value in kwargs.items():
                setattr(self, key, value)

        @classmethod
        def model_validate(cls, payload: Any) -> "ChatCompletionRequestMessage":
            if isinstance(payload, cls):
                return payload
            if isinstance(payload, dict):
                return cls(**payload)
            return cls(role="assistant", content=str(payload))

    CreateChatCompletionRequest = Any

try:
    import mcp.types
except ImportError:  # pragma: no cover - optional dependency support
    mcp = Any

from mcp_bridge.mcp_clients.AbstractClient import DEFAULT_MCP_SESSION_TIMEOUT_SECONDS
from mcp_bridge.mcp_clients.McpClientManager import ClientManager, DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
from mcp_bridge.tool_mappers import mcp2openai

def maybe_add_tool_selection_instructions(request: Any) -> Any:
    tool_names: list[str] = []
    for tool in getattr(request, "tools", []) or []:
        if isinstance(tool, dict):
            function_payload = tool.get("function") or {}
            if isinstance(function_payload, dict):
                tool_name = function_payload.get("name") or tool.get("name")
            else:
                tool_name = getattr(function_payload, "name", None) or tool.get("name")
        else:
            tool_name = getattr(tool, "name", None)
            if tool_name is None and hasattr(tool, "function"):
                tool_name = getattr(getattr(tool, "function"), "name", None)
        if tool_name is not None:
            tool_names.append(str(tool_name))

    has_github_search_tool = any(name == "searchGitHub" or "github" in name.lower() for name in tool_names)
    if not has_github_search_tool:
        return request

    messages = list(getattr(request, "messages", []) or [])
    if not messages:
        return request

    text_chunks = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            text_chunks.extend(str(part) for part in content if part is not None)
        elif content is not None:
            text_chunks.append(str(content))

    combined_text = "\n".join(text_chunks).lower()
    is_code_search_prompt = any(
        phrase in combined_text
        for phrase in [
            "example",
            "code",
            "implementation",
            "repository",
            "github",
            "pattern",
            "snippet",
            "usage",
            "library",
        ]
    )
    if not is_code_search_prompt:
        return request

    if any(getattr(message, "role", None) == "system" and "searchGitHub" in str(getattr(message, "content", "")) for message in messages):
        return request

    instruction = (
        "When the user asks for implementation examples, code patterns, or real repository-based examples, "
        "prefer using the searchGitHub tool before answering from memory. "
        "Use searchGitHub for concrete code/example searches and cite the result in your answer."
    )

    system_message = SimpleNamespace(role="system", content=instruction)

    request.messages = [system_message, *messages]
    return request


def get_tool_discovery_timeout_seconds() -> float:
    raw_value = os.getenv("MCP_BRIDGE_TOOL_DISCOVERY_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_MCP_SESSION_TIMEOUT_SECONDS

    try:
        return float(raw_value)
    except ValueError:
        logger.warning(
            f"invalid MCP_BRIDGE_TOOL_DISCOVERY_TIMEOUT_SECONDS value: {raw_value}; using default {DEFAULT_MCP_SESSION_TIMEOUT_SECONDS}"
        )
        return DEFAULT_MCP_SESSION_TIMEOUT_SECONDS


async def _ensure_client_manager_initialized() -> list[tuple[str, Any]]:
    clients = ClientManager.get_clients()
    if clients:
        return clients

    logger.info("No MCP clients initialized yet; initializing client manager before tool discovery")
    await ClientManager.initialize()
    return ClientManager.get_clients()


# Cache of tool name -> inputSchema (JSON Schema) discovered at request time.
# Used by `repair_tool_arguments` to coerce/fill LLM-produced arguments before
# they are forwarded to the MCP server, reducing "(failed validation)" errors.
#
# Bounded LRU cache (keyed by tool name) so a long-running process with many MCP
# servers does not grow this dict without bound, and so a server whose tools
# change over time does not leave stale schemas cached forever.
_TOOL_SCHEMA_CACHE_MAX_ENTRIES = 512
_TOOL_SCHEMA_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _cache_tool_schema(tool_name: str, input_schema: Any) -> None:
    if not tool_name:
        return
    if isinstance(input_schema, dict):
        key = str(tool_name)
        # Re-insert to mark as most-recently-used, then evict the oldest entry
        # if the cache exceeds its bound.
        _TOOL_SCHEMA_CACHE.pop(key, None)
        _TOOL_SCHEMA_CACHE[key] = input_schema
        while len(_TOOL_SCHEMA_CACHE) > _TOOL_SCHEMA_CACHE_MAX_ENTRIES:
            _TOOL_SCHEMA_CACHE.popitem(last=False)


async def chat_completion_add_tools(request: CreateChatCompletionRequest):
    request.tools = []

    tool_discovery_timeout_seconds = get_tool_discovery_timeout_seconds()
    clients = await _ensure_client_manager_initialized()

    async def _discover_tools_for_session(session: Any) -> list[Any]:
        configured_request_timeout = None
        config = getattr(session, "config", None)
        request_timeout = getattr(config, "requestTimeout", None)
        if request_timeout is not None:
            configured_request_timeout = float(request_timeout) / 1000.0

        wait_timeout = float(tool_discovery_timeout_seconds)
        if configured_request_timeout is not None:
            wait_timeout = max(wait_timeout, configured_request_timeout)

        try:
            await session._wait_for_session(timeout=wait_timeout, http_error=False)
        except Exception:
            logger.warning(f"session not ready for {session.name}; skipping tool discovery")
            return []

        if session.session is None:
            logger.error(f"session is `None` for {session.name}")
            return []

        try:
            tools = await asyncio.wait_for(session.session.list_tools(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"tool discovery timed out for {session.name} after {wait_timeout:.1f}s "
                f"(server did not respond to tools/list)"
            )
            return []
        except Exception as exc:
            exc_repr = str(exc) or type(exc).__name__
            logger.warning(f"tool discovery failed for {session.name}: {exc_repr}")
            return []

        try:
            converted = []
            for tool in tools.tools:
                _cache_tool_schema(getattr(tool, "name", None), getattr(tool, "inputSchema", None))
                converted.append(mcp2openai(tool))
            return converted
        except Exception as exc:
            # A single malformed tool schema from one server must not abort
            # tool discovery for the whole request. Skip this server's tools
            # and let the others proceed.
            exc_repr = str(exc) or type(exc).__name__
            logger.warning(f"tool conversion failed for {session.name}: {exc_repr}")
            return []

    # `return_exceptions=True` is defense-in-depth: even if a discovery task
    # raises unexpectedly (e.g. a bug in a transport), one server's failure
    # must not abort the entire request. Exceptions are logged and skipped.
    discovered_tools = await asyncio.gather(
        *(_discover_tools_for_session(session) for _, session in clients),
        return_exceptions=True,
    )

    for tools in discovered_tools:
        if isinstance(tools, BaseException):
            logger.warning(f"tool discovery task raised: {tools!r}")
            continue
        request.tools.extend(tools)

    maybe_add_tool_selection_instructions(request)
    return request


DEFAULT_MAX_SEARCH_RESULTS = 5

tracer = trace.get_tracer("mcp_bridge.openai_clients.utils")


def _is_search_tool(tool_name: str | None) -> bool:
    if tool_name is None:
        return False

    normalized_name = tool_name.lower()
    return normalized_name == "search" or normalized_name in {"searchgithub", "search_web", "web_search", "websearch"}


def get_max_search_results() -> int:
    raw_value = os.getenv("MCP_BRIDGE_MAX_SEARCH_RESULTS")
    if raw_value is None:
        return DEFAULT_MAX_SEARCH_RESULTS

    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning(f"invalid MCP_BRIDGE_MAX_SEARCH_RESULTS value: {raw_value}; using default {DEFAULT_MAX_SEARCH_RESULTS}")
        return DEFAULT_MAX_SEARCH_RESULTS


def clamp_search_tool_arguments(tool_name: str | None, arguments: Any) -> Any:
    if not isinstance(arguments, dict) or not _is_search_tool(tool_name):
        return arguments

    max_results = get_max_search_results()
    if "max_results" not in arguments:
        arguments = dict(arguments)
        arguments["max_results"] = max_results
        return arguments

    try:
        requested_max_results = int(arguments["max_results"])
    except (TypeError, ValueError):
        requested_max_results = max_results

    clamped = min(requested_max_results, max_results)
    if clamped < 1:
        clamped = 1

    updated_arguments = dict(arguments)
    updated_arguments["max_results"] = clamped
    return updated_arguments


# Per-tool default overrides for required fields that LLMs frequently omit.
# Keyed by normalized tool name; each entry maps a missing required field to a
# sensible default value. This is a pragmatic fallback for tools whose schemas
# are strict but whose required fields are almost always safe to default.
_TOOL_REQUIRED_DEFAULTS: dict[str, dict[str, Any]] = {
    "sequentialthinking": {
        "nextThoughtNeeded": True,
    },
    "sequential-thinking": {
        "nextThoughtNeeded": True,
    },
}


def _value_matches_type(value: Any, schema: Any) -> bool:
    """Return True if ``value`` already conforms to the schema's declared type."""
    if not isinstance(schema, dict):
        return True
    for combinator in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list) and branches:
            return any(_value_matches_type(value, branch) for branch in branches)
    type_spec = schema.get("type")
    if isinstance(type_spec, list):
        return any(_value_matches_type(value, {"type": t}) for t in type_spec)
    if type_spec == "boolean":
        return isinstance(value, bool)
    if type_spec == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_spec == "string":
        return isinstance(value, str)
    if type_spec == "array":
        return isinstance(value, list)
    if type_spec == "object":
        return isinstance(value, dict)
    return True


def _coerce_value(value: Any, schema: Any) -> Any:
    """Coerce a single value to the type described by a JSON-schema fragment."""
    if not isinstance(schema, dict):
        return value

    # Resolve anyOf/oneOf/allOf by picking the first branch that accepts the
    # value (or the first branch's type coercion).
    for combinator in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list) and branches:
            # Prefer a branch whose declared type already matches the value
            # (no coercion needed), so e.g. an int stays an int even when a
            # string branch appears first.
            for branch in branches:
                if _value_matches_type(value, branch):
                    return value
            for branch in branches:
                coerced = _coerce_value(value, branch)
                if coerced is not None:
                    return coerced
            return value

    type_spec = schema.get("type")
    if isinstance(type_spec, list):
        # e.g. ["string", "null"] -> pick the first non-null type
        non_null = [t for t in type_spec if t != "null"]
        type_spec = non_null[0] if non_null else "null"

    if type_spec == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y"}:
                return True
            if lowered in {"false", "0", "no", "n"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return value

    if type_spec == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except (TypeError, ValueError):
                try:
                    return int(float(value.strip()))
                except (TypeError, ValueError):
                    return value
        return value

    if type_spec == "number":
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                return value
        return value

    if type_spec == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (bool, int, float)):
            return str(value)
        return value

    if type_spec == "array":
        if isinstance(value, list):
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                return [_coerce_value(item, item_schema) for item in value]
            return value
        if isinstance(value, str):
            # Some models send a JSON-encoded array as a string.
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return value

    if type_spec == "object":
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return value

    return value


def _default_for_schema(schema: Any) -> Any:
    """Return a default value for a JSON-schema fragment, or None if unknown."""
    if not isinstance(schema, dict):
        return None
    if "default" in schema:
        return schema["default"]
    for combinator in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(combinator)
        if isinstance(branches, list) and branches:
            for branch in branches:
                default = _default_for_schema(branch)
                if default is not None:
                    return default
            return None
    type_spec = schema.get("type")
    if isinstance(type_spec, list):
        non_null = [t for t in type_spec if t != "null"]
        type_spec = non_null[0] if non_null else "null"
    if type_spec == "boolean":
        return False
    if type_spec == "integer":
        return 0
    if type_spec == "number":
        return 0.0
    if type_spec == "string":
        return ""
    if type_spec == "array":
        return []
    if type_spec == "object":
        return {}
    return None


def repair_tool_arguments(tool_name: str | None, arguments: Any) -> Any:
    """Repair/coerce LLM-produced tool arguments against the tool's inputSchema.

    LLMs frequently omit required fields, add unknown keys, or emit wrong types
    (e.g. ``"true"`` instead of ``true``). MCP servers validate strictly and
    reject such calls with ``(failed validation)``. This function normalizes the
    arguments before they are forwarded:

    * drops unknown keys when ``additionalProperties`` is ``false``
    * coerces values to the declared JSON-schema types
    * fills missing required fields with schema defaults (or per-tool defaults)

    Returns the (possibly unchanged) arguments dict.
    """
    if not isinstance(arguments, dict):
        return arguments

    schema = _TOOL_SCHEMA_CACHE.get(str(tool_name or ""))
    if not isinstance(schema, dict):
        return arguments

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return arguments

    required = schema.get("required")
    required_set = set(required) if isinstance(required, list) else set()

    additional_properties = schema.get("additionalProperties", True)
    reject_unknown = additional_properties is False

    repaired: dict[str, Any] = {}
    for key, value in arguments.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            if reject_unknown:
                continue
            repaired[key] = value
            continue
        repaired[key] = _coerce_value(value, prop_schema)

    # Fill missing required fields.
    for key in required_set:
        if key in repaired:
            continue
        prop_schema = properties.get(key)
        # Per-tool override takes precedence (e.g. sequential-thinking's
        # nextThoughtNeeded should default to True, not the generic False).
        default = _TOOL_REQUIRED_DEFAULTS.get(str(tool_name or ""), {}).get(key)
        if default is None:
            default = _default_for_schema(prop_schema)
        if default is not None:
            repaired[key] = default

    return repaired


def truncate_search_result_text(tool_name: str | None, text: str | None, max_results: int | None = None) -> str | None:
    if not isinstance(text, str) or not _is_search_tool(tool_name):
        return text

    result_limit = max_results if max_results is not None else get_max_search_results()
    if result_limit < 1:
        result_limit = 1

    pattern = re.compile(r"(?m)^\s*(\d+)\.\s")
    matches = list(pattern.finditer(text))
    if len(matches) <= result_limit:
        return text

    kept_parts: list[str] = []
    for index in range(result_limit):
        start = matches[index].start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        part = text[start:end].strip()
        if part:
            kept_parts.append(part)

    summary = "\n\n".join(kept_parts)
    return (
        f"Showing the first {result_limit} search results only; additional results were omitted to avoid overwhelming the tool loop.\n\n"
        + summary
    )


def sanitize_tool_result_content(tool_name: str | None, tool_call_result: Any, max_results: int | None = None) -> list[dict[str, str]]:
    if not hasattr(tool_call_result, "content"):
        return []

    text_parts: list[dict[str, str]] = []
    for part in getattr(tool_call_result, "content", []):
        if getattr(part, "type", None) != "text":
            continue

        original_text = getattr(part, "text", "") or ""
        sanitized_text = truncate_search_result_text(tool_name, original_text, max_results=max_results)
        if sanitized_text is None:
            sanitized_text = original_text

        text_parts.append({"type": "text", "text": sanitized_text})

    if not text_parts:
        return [{"type": "text", "text": "the tool call result is empty"}]

    return text_parts


def _span_payload_preview(payload: Any, max_len: int = 160) -> str:
    if payload is None:
        return "null"

    try:
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        rendered = str(payload)

    if len(rendered) <= max_len:
        return rendered

    return rendered[:max_len] + "...[truncated]"


def _parse_lenient_json(raw: str) -> Any:
    """Parse LLM-produced JSON, tolerating common malformations.

    LLMs frequently emit arguments that are not strictly valid JSON: trailing
    commas, single-quoted strings, unquoted keys, or a leading/trailing code
    fence. This tries strict parsing first, then progressively more lenient
    fallbacks. Returns the parsed value, or ``None`` if it cannot be recovered.
    """
    if not isinstance(raw, str):
        return raw

    text = raw.strip()
    if not text:
        return None

    # Strip a surrounding markdown code fence if present.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 1. Strict JSON.
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Python literal (handles single quotes, trailing commas, bare True/False).
    try:
        import ast

        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        pass

    # 3. Repair pass: strip trailing commas, convert single quotes to double
    #    quotes (only outside strings), and wrap unquoted keys.
    repaired = _repair_json_text(text)
    if repaired is not None:
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _repair_json_text(text: str) -> str | None:
    """Best-effort repair of common JSON syntax errors. Returns None on failure."""
    if not isinstance(text, str) or not text.strip():
        return None

    out: list[str] = []
    in_string = False
    escape = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch in " \t\r\n":
            out.append(ch)
            i += 1
            continue

        if ch == "'":
            # Convert single-quoted string to double-quoted.
            out.append('"')
            in_string = True
            i += 1
            continue

        if ch == ",":
            # Drop a trailing comma before } or ].
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
            out.append(ch)
            i += 1
            continue

        if ch in "{}[]":
            out.append(ch)
            i += 1
            continue

        if ch == ":":
            out.append(ch)
            i += 1
            continue

        # Unquoted key: a bare identifier followed by ':'.
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_"):
                j += 1
            word = text[i:j]
            k = j
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] == ":":
                out.append('"')
                out.append(word)
                out.append('"')
                i = j
                continue
            # Bare True/False/None literal.
            if word in {"True", "False", "None"}:
                out.append(word)
                i = j
                continue
            out.append(word)
            i = j
            continue

        out.append(ch)
        i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# Fetch mirror/cache fallback
#
# The `fetch` MCP tool respects robots.txt and can be blocked by server-side
# bot protection (Cloudflare 403, etc.). When a fetch fails for one of these
# reasons, MCP-Bridge transparently retries the URL via the Internet Archive
# Wayback Machine and returns the archived content (clearly labelled) so the
# LLM still gets usable evidence instead of a hard failure.
# ---------------------------------------------------------------------------

DEFAULT_FETCH_MIRROR_TIMEOUT_SECONDS = 30.0
WAYBACK_AVAILABILITY_API = "https://archive.org/wayback/available"


def get_fetch_mirror_fallback_enabled() -> bool:
    raw = os.getenv("MCP_BRIDGE_FETCH_MIRROR_FALLBACK")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_fetch_mirror_timeout_seconds() -> float:
    raw = os.getenv("MCP_BRIDGE_FETCH_MIRROR_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_FETCH_MIRROR_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_FETCH_MIRROR_TIMEOUT_SECONDS


def _is_fetch_tool(tool_name: str | None) -> bool:
    if not tool_name:
        return False
    return tool_name.strip().lower() in {"fetch", "fetch_content", "web_fetch"}


def _is_fetch_blocked_error(error_text: str | None) -> bool:
    """Return True if a fetch error is a block (robots.txt / 403 / bot detection)
    that a mirror/cache fallback could plausibly bypass."""
    if not error_text:
        return False
    lowered = error_text.lower()
    block_markers = (
        "robots.txt",
        "robots txt",
        "not allowed",
        "not permitted",
        "status code 403",
        "status 403",
        "403 forbidden",
        "blocked",
        "bot detection",
        "bot protection",
        "cloudflare",
        "checking your browser",
        "just a moment",
        "access denied",
        "forbidden",
        "captcha",
    )
    return any(marker in lowered for marker in block_markers)


def _extract_fetch_url(arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return None
    url = arguments.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


async def _fetch_via_wayback(url: str, timeout: float | None = None) -> CallToolResult | None:
    """Attempt to fetch ``url`` via the Internet Archive Wayback Machine.

    Returns a successful ``CallToolResult`` with the archived content, or None
    if no snapshot is available / the archive fetch fails.
    """
    effective_timeout = timeout if timeout is not None else get_fetch_mirror_timeout_seconds()

    try:
        async with httpx.AsyncClient(timeout=effective_timeout, follow_redirects=True) as client:
            # 1. Query the availability API for the closest snapshot.
            availability = await client.get(
                WAYBACK_AVAILABILITY_API,
                params={"url": url},
            )
            if availability.status_code != 200:
                logger.debug(f"wayback availability API returned {availability.status_code} for {url}")
                return None

            data = availability.json()
            snapshot = (data.get("archived_snapshots") or {}).get("closest")
            if not snapshot or not snapshot.get("url"):
                logger.debug(f"no wayback snapshot available for {url}")
                return None

            snapshot_url = snapshot["url"]
            logger.info(f"fetch blocked; retrying via wayback snapshot: {snapshot_url}")

            # 2. Fetch the archived snapshot content.
            response = await client.get(snapshot_url)
            if response.status_code != 200:
                logger.debug(f"wayback snapshot fetch returned {response.status_code} for {snapshot_url}")
                return None

            text = response.text
            if not text or len(text.strip()) < 20:
                logger.debug(f"wayback snapshot content too short for {snapshot_url}")
                return None

            # Trim to a reasonable size to avoid overwhelming the tool loop.
            max_chars = 20000
            if len(text) > max_chars:
                text = text[:max_chars] + "\n...[truncated]"

            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f"[Fetched via Internet Archive Wayback Machine snapshot of {url}]\n\n"
                            f"Source: {snapshot_url}\n\n"
                            f"{text}"
                        ),
                    )
                ],
                isError=False,
            )
    except Exception as exc:
        logger.debug(f"wayback fallback failed for {url}: {exc}")
        return None


def _normalize_query_text(text: str) -> str:
    """Collapse whitespace and lowercase for stable query comparison."""
    return " ".join(str(text).split()).lower()


def _extract_query_argument(payload: str) -> str:
    """Extract the ``query`` argument from a tool-call JSON payload, if present."""
    if not payload:
        return ""
    try:
        args = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(args, dict):
        return ""
    query = args.get("query")
    if isinstance(query, str):
        return query.strip()
    return ""


def _queries_are_near_duplicates(a: str, b: str, *, min_shared: int = 4, overlap_ratio: float = 0.8) -> bool:
    """Return True if two search queries are near-duplicates.

    A model stuck in a search loop often re-issues essentially the same query
    with one keyword appended or removed (e.g. adding "الكفار", "تشبه",
    "محدثة" one at a time). Exact-match detection misses this. Here we treat
    two queries as near-duplicates when they share a large fraction of their
    terms, which lets the cache serve the second query instead of re-fetching
    from the web.

    ``min_shared`` guards against short queries (two 2-word queries sharing 1
    word are not duplicates). ``overlap_ratio`` is the fraction of the smaller
    query's terms that must appear in the larger one.
    """
    a_terms = set(_normalize_query_text(a).split())
    b_terms = set(_normalize_query_text(b).split())
    if not a_terms or not b_terms:
        return False
    shared = a_terms & b_terms
    if len(shared) < min_shared:
        return False
    smaller = min(len(a_terms), len(b_terms))
    return len(shared) / smaller >= overlap_ratio


class ToolResultCache:
    """Per-request cache of tool results keyed by (tool_name, normalized query).

    Near-identical search queries (the "append one keyword" loop pattern) hit
    this cache and return the previously fetched result instantly, instead of
    re-fetching from the web and burning 30+ minutes. This lets the model keep
    exploring from different angles while making repeated/near-repeated queries
    cheap, so the loop converges on its own.
    """

    def __init__(self, *, max_entries: int = 64) -> None:
        self._entries: list[tuple[str, str, Any]] = []
        self._max_entries = max_entries

    def _find(self, tool_name: str, query: str) -> Any | None:
        for cached_name, cached_query, result in self._entries:
            if cached_name != tool_name:
                continue
            if _queries_are_near_duplicates(cached_query, query):
                return result
        return None

    def get(self, tool_name: str, query: str) -> Any | None:
        return self._find(tool_name, query)

    def put(self, tool_name: str, query: str, result: Any) -> None:
        if result is None:
            return
        # Don't cache error results (works for both objects and dicts).
        if isinstance(result, dict):
            if result.get("isError"):
                return
        elif getattr(result, "isError", False):
            return
        # Avoid caching the same query twice.
        if self._find(tool_name, query) is not None:
            return
        self._entries.append((tool_name, query, result))
        if len(self._entries) > self._max_entries:
            self._entries.pop(0)

    def __len__(self) -> int:
        return len(self._entries)


# Default TTL for the persistent on-disk tool cache (48 hours).
DEFAULT_TOOL_CACHE_TTL_SECONDS = 48 * 60 * 60


def get_tool_cache_dir() -> str:
    """Return the directory for the persistent on-disk tool cache."""
    configured = os.getenv("MCP_BRIDGE_TOOL_CACHE_DIR", "")
    if configured:
        return configured
    # Default to a directory next to the repo root.
    return os.path.join(os.path.dirname(__file__), "..", "..", "tool_cache")


def get_tool_cache_ttl_seconds() -> int:
    raw = os.getenv("MCP_BRIDGE_TOOL_CACHE_TTL_SECONDS")
    if raw is None:
        return DEFAULT_TOOL_CACHE_TTL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            f"invalid MCP_BRIDGE_TOOL_CACHE_TTL_SECONDS value: {raw}; using default {DEFAULT_TOOL_CACHE_TTL_SECONDS}"
        )
        return DEFAULT_TOOL_CACHE_TTL_SECONDS


def get_tool_cache_enabled() -> bool:
    raw = os.getenv("MCP_BRIDGE_TOOL_CACHE_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _tool_cache_key(tool_name: str, query: str) -> str:
    """Stable hash key for a (tool_name, query) pair."""
    raw = f"{tool_name}::{_normalize_query_text(query)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PersistentToolCache:
    """On-disk, TTL-bounded cache of tool results keyed by exact (tool_name, query).

    Unlike the in-memory ``ToolResultCache`` (which catches near-duplicate
    queries within a single request), this cache persists across requests and
    process restarts. It uses exact-match keys (a SHA-256 hash of the query) so
    a lookup is a single file read — no scanning, no RAM bloat. A TTL (default
    48h) expires stale entries so search results don't go stale.

    This is especially valuable because fetching from the web is the biggest
    latency source: if a request fails halfway, the next attempt reuses the
    already-fetched results instead of re-fetching them.
    """

    def __init__(self, cache_dir: str | None = None, ttl_seconds: int | None = None) -> None:
        self._cache_dir = os.path.abspath(cache_dir or get_tool_cache_dir())
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else get_tool_cache_ttl_seconds()
        self._enabled = get_tool_cache_enabled()
        if self._enabled:
            try:
                Path(self._cache_dir).mkdir(parents=True, exist_ok=True)
                # Verify the directory is actually *writable*, not just present.
                # A bind-mounted directory (e.g. `./tool_cache:/app/tool_cache`)
                # may exist but be owned by a different UID — e.g. when the
                # container runs as an unprivileged user. Detect this upfront
                # and disable the cache gracefully instead of failing on every
                # single write (which would otherwise spam DEBUG logs).
                test_file = Path(self._cache_dir) / ".write_test"
                test_file.write_text("ok", encoding="utf-8")
                test_file.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"tool cache dir not writable ({exc}); disabling persistent cache")
                self._enabled = False

    def _path_for(self, key: str) -> str:
        return os.path.join(self._cache_dir, f"{key}.json")

    def get(self, tool_name: str, query: str) -> Any | None:
        if not self._enabled or not query:
            return None
        key = _tool_cache_key(tool_name, query)
        path = self._path_for(key)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

        cached_at = payload.get("cached_at", 0)
        if time.time() - cached_at > self._ttl_seconds:
            # Expired: remove and treat as a miss.
            try:
                os.remove(path)
            except OSError:
                pass
            return None

        try:
            return CallToolResult.model_validate(payload.get("result"))
        except Exception:
            return None

    def put(self, tool_name: str, query: str, result: Any) -> None:
        if not self._enabled or not query or result is None:
            return
        # Don't cache error results.
        if isinstance(result, dict):
            if result.get("isError"):
                return
        elif getattr(result, "isError", False):
            return

        try:
            if isinstance(result, dict):
                result_dict = result
            else:
                result_dict = result.model_dump(exclude_defaults=True, exclude_none=True)
        except Exception:
            return

        payload = {
            "tool_name": tool_name,
            "query": query,
            "cached_at": time.time(),
            "result": result_dict,
        }
        key = _tool_cache_key(tool_name, query)
        path = self._path_for(key)
        try:
            # Atomic write: write to a temp file then rename, so concurrent
            # requests never observe a partially-written cache file.
            fd, tmp_path = tempfile.mkstemp(dir=self._cache_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.debug(f"tool cache write failed: {exc}")


async def call_tool(
    tool_call_name: str, tool_call_json: str, timeout: int | None = None,
    trace_logger: RequestTraceLogger | None = None,
    client_cache: dict[str, Any] | None = None,
) -> Any | None:
    with tracer.start_as_current_span("mcp_bridge.call_tool") as span:
        span.set_attribute("mcp_bridge.tool.name", tool_call_name or "")
        span.set_attribute("mcp_bridge.tool.arguments.length", len(tool_call_json or ""))
        span.set_attribute("mcp_bridge.tool.arguments.preview", _span_payload_preview(tool_call_json))
        span.set_attribute("mcp_bridge.tool.arguments.json_valid", bool(tool_call_json is not None))
        span.set_attribute("mcp_bridge.tool.timeout_seconds", float(timeout or 0))

        if tool_call_name == "" or tool_call_name is None:
            logger.error("tool call name is empty")
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name or "<empty>",
                    is_error=True,
                    reason="empty_tool_name",
                )
            return None

        if tool_call_json is None:
            logger.error("tool call json is empty")
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason="empty_tool_arguments",
                )
            return None

        if trace_logger is not None:
            trace_logger.record("mcp_tool_dispatch_attempt", tool_name=tool_call_name, arguments=tool_call_json)

        if client_cache is not None and tool_call_name in client_cache:
            session = client_cache[tool_call_name]
            resolved_tool_name = tool_call_name
        else:
            resolved = await ClientManager.resolve_tool(
                tool_call_name, timeout=DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
            )
            if resolved is None:
                session = None
                resolved_tool_name = tool_call_name
            else:
                session, resolved_tool_name = resolved
            if client_cache is not None:
                client_cache[tool_call_name] = session

        if session is None:
            logger.error(f"no MCP client found for tool '{tool_call_name}'")
            span.set_attribute("mcp_bridge.tool.client_found", False)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "no_mcp_client"))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason="no_mcp_client",
                )
            class ToolDispatchError:
                isError = True

                def __init__(self, message: str) -> None:
                    self.content = [type("ToolTextContent", (), {"type": "text", "text": message})()]

                def model_dump(self, **kwargs: Any) -> dict[str, Any]:
                    return {"isError": True, "content": [{"type": "text", "text": self.content[0].text}]}

            # Build a corrective message the model can act on instead of a
            # dead-end. The model named a tool that doesn't exist (often a
            # server name or a misname); listing close matches lets it
            # self-correct on the next loop iteration.
            suggestions = await ClientManager.suggest_tools(
                tool_call_name, timeout=DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
            )
            if suggestions:
                suggestion_text = ", ".join(suggestions)
                message = (
                    f"'{tool_call_name}' is not a registered tool. "
                    f"Did you mean one of: {suggestion_text}? "
                    f"Call one of these exact names instead."
                )
            else:
                message = (
                    f"No MCP client found for tool '{tool_call_name}'. "
                    f"Only call tools by their exact registered names."
                )
            return ToolDispatchError(message)

        try:
            tool_call_args = _parse_lenient_json(tool_call_json)
            if not isinstance(tool_call_args, dict):
                raise ValueError("tool arguments must be a JSON object")
            span.set_attribute("mcp_bridge.tool.arguments.parsed", True)
            span.set_attribute("mcp_bridge.tool.arguments.keys", ",".join(sorted(str(key) for key in tool_call_args.keys())))
        except (json.JSONDecodeError, ValueError):
            logger.error(f"failed to decode json for {tool_call_name}: {tool_call_json[:200]}")
            span.set_attribute("mcp_bridge.tool.arguments.parsed", False)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "invalid_json"))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason="invalid_json",
                )
            return None

        try:
            span.set_attribute("mcp_bridge.tool.client_name", getattr(session, "name", ""))
            repaired_args = repair_tool_arguments(tool_call_name, tool_call_args)
            repaired_args = clamp_search_tool_arguments(tool_call_name, repaired_args)
            if repaired_args != tool_call_args:
                logger.debug(
                    f"repaired tool arguments for {tool_call_name}: "
                    f"{_span_payload_preview(tool_call_args)} -> {_span_payload_preview(repaired_args)}"
                )
                span.set_attribute("mcp_bridge.tool.arguments.repaired", True)
            if resolved_tool_name != tool_call_name:
                logger.debug(
                    f"resolved tool name for {tool_call_name} -> {resolved_tool_name}"
                )
                span.set_attribute("mcp_bridge.tool.resolved_name", resolved_tool_name)
            result = await session.call_tool(resolved_tool_name, repaired_args, timeout)
        except Exception as exc:
            logger.error(f"tool dispatch failed for {tool_call_name}: {exc}")
            span.set_attribute("mcp_bridge.tool.client_name", getattr(session, "name", ""))
            span.set_attribute("mcp_bridge.tool.result.is_error", True)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason=str(exc),
                )
            return None

        span.set_attribute("mcp_bridge.tool.client_found", True)
        span.set_attribute("mcp_bridge.tool.result.is_error", bool(getattr(result, "isError", False)))
        span.set_attribute("mcp_bridge.tool.result.preview", _span_payload_preview(result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) if hasattr(result, "model_dump") else result))
        if trace_logger is not None:
            trace_logger.record(
                "mcp_tool_dispatch_result",
                tool_name=tool_call_name,
                is_error=getattr(result, "isError", False),
                result=result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) if hasattr(result, "model_dump") else result,
            )

        # Mirror/cache fallback: if a fetch tool call was blocked (robots.txt,
        # 403, bot detection), transparently retry via the Wayback Machine so
        # the LLM still gets usable content instead of a hard failure.
        if (
            getattr(result, "isError", False)
            and _is_fetch_tool(tool_call_name)
            and get_fetch_mirror_fallback_enabled()
        ):
            error_text = next(
                (
                    part.text
                    for part in getattr(result, "content", [])
                    if getattr(part, "type", None) == "text"
                ),
                "",
            )
            url = _extract_fetch_url(tool_call_args)
            if _is_fetch_blocked_error(error_text) and url:
                logger.warning(
                    f"fetch blocked for {url}; attempting wayback mirror fallback"
                )
                mirror_result = await _fetch_via_wayback(url)
                if mirror_result is not None:
                    span.set_attribute("mcp_bridge.tool.mirror_fallback", True)
                    if trace_logger is not None:
                        trace_logger.record(
                            "mcp_tool_mirror_fallback",
                            tool_name=tool_call_name,
                            url=url,
                            is_error=False,
                        )
                    return mirror_result

        return result


async def call_tools(
    tool_calls: list[tuple[str, str]], timeout: int | None = None,
    trace_logger: RequestTraceLogger | None = None,
    client_cache: dict[str, Any] | None = None,
    result_cache: ToolResultCache | None = None,
    persistent_cache: PersistentToolCache | None = None,
) -> list[Any]:
    """Execute multiple tool calls concurrently while preserving order.

    If ``result_cache`` is provided, near-identical search queries are served
    from the in-memory cache. If ``persistent_cache`` is provided, exact-match
    queries are served from the on-disk cache (across requests).
    """

    if not tool_calls:
        return []

    async def _run(call: tuple[str, str]) -> Any:
        name, payload = call
        query = _extract_query_argument(payload)

        # 1. In-memory fuzzy cache (near-duplicate queries within this request).
        if result_cache is not None and query:
            cached = result_cache.get(name, query)
            if cached is not None:
                logger.debug(f"tool call served from in-memory cache: name={name}; query={query[:80]}")
                return cached

        # 2. Persistent on-disk cache (exact-match queries across requests).
        if persistent_cache is not None and query:
            cached = persistent_cache.get(name, query)
            if cached is not None:
                logger.debug(f"tool call served from persistent cache: name={name}; query={query[:80]}")
                return cached

        call_kwargs = {"timeout": timeout}
        if trace_logger is not None:
            call_kwargs["trace_logger"] = trace_logger

        signature = inspect.signature(call_tool)
        if "trace_logger" in signature.parameters:
            call_kwargs["client_cache"] = client_cache
            result = await call_tool(name, payload, **call_kwargs)
        else:
            result = await call_tool(name, payload, timeout)

        if query:
            if result_cache is not None:
                result_cache.put(name, query, result)
            if persistent_cache is not None:
                persistent_cache.put(name, query, result)
        return result

    return await asyncio.gather(*(_run(call) for call in tool_calls))
