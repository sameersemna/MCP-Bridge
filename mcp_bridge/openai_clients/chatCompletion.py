import os
import re
import time
from typing import Any

from fastapi import HTTPException, Request
from lmos_openai_types import (
    CreateChatCompletionRequest,
    CreateChatCompletionResponse,
    ChatCompletionRequestMessage,
    FinishReason1,
)

from .utils import call_tools, chat_completion_add_tools, sanitize_tool_result_content
from .genericHttpxClient import get_client
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.tool_mappers import mcp2openai
from mcp_bridge.logging import RequestTraceLogger
from loguru import logger
import json

DEFAULT_MAX_TOOL_TURNS = 12
MIN_MAX_TOOL_TURNS = 12
DEFAULT_TOOL_TIMEOUT_SECONDS = 60
# Safety cap on the accumulated prompt context (in tokens) for a single
# tool-calling request. Prevents runaway loops where the model keeps issuing
# tool calls and the context grows unboundedly (e.g. 3+ hour requests).
DEFAULT_MAX_CONTEXT_TOKENS = 60000


def get_max_context_tokens() -> int:
    raw_value = os.getenv("MCP_BRIDGE_MAX_CONTEXT_TOKENS")
    if raw_value is None:
        return DEFAULT_MAX_CONTEXT_TOKENS

    try:
        configured_value = int(raw_value)
    except ValueError:
        logger.warning(
            f"invalid MCP_BRIDGE_MAX_CONTEXT_TOKENS value: {raw_value}; using default {DEFAULT_MAX_CONTEXT_TOKENS}"
        )
        return DEFAULT_MAX_CONTEXT_TOKENS

    if configured_value < 1000:
        logger.warning(
            f"configured MCP_BRIDGE_MAX_CONTEXT_TOKENS={configured_value} is below the safe minimum 1000; using 1000"
        )
        return 1000

    return configured_value


def get_tool_timeout_seconds() -> int:
    raw_value = os.getenv("MCP_BRIDGE_TOOL_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_TOOL_TIMEOUT_SECONDS

    try:
        return int(raw_value)
    except ValueError:
        logger.warning(f"invalid MCP_BRIDGE_TOOL_TIMEOUT_SECONDS value: {raw_value}; using default {DEFAULT_TOOL_TIMEOUT_SECONDS}")
        return DEFAULT_TOOL_TIMEOUT_SECONDS


def get_max_tool_turns() -> int:
    raw_value = os.getenv("MCP_BRIDGE_MAX_TOOL_TURNS")
    if raw_value is None:
        return DEFAULT_MAX_TOOL_TURNS

    try:
        configured_value = int(raw_value)
    except ValueError:
        logger.warning(f"invalid MCP_BRIDGE_MAX_TOOL_TURNS value: {raw_value}; using default {DEFAULT_MAX_TOOL_TURNS}")
        return DEFAULT_MAX_TOOL_TURNS

    if configured_value < MIN_MAX_TOOL_TURNS:
        logger.warning(
            f"configured MCP_BRIDGE_MAX_TOOL_TURNS={configured_value} is below the safe minimum {MIN_MAX_TOOL_TURNS}; using {MIN_MAX_TOOL_TURNS}"
        )
        return MIN_MAX_TOOL_TURNS

    return configured_value


def _summarize_trace(trace_logger: RequestTraceLogger) -> dict[str, object]:
    events = trace_logger.events
    return {
        "event_count": len(events),
        "last_event_type": events[-1]["type"] if events else None,
        "tool_events": sum(1 for event in events if event["type"] in {"mcp_tool_calls", "mcp_tool_result"}),
        "llm_responses": sum(1 for event in events if event["type"] == "llm_response"),
    }


def should_continue_tool_loop(
    finish_reason: str | None,
    *,
    tool_call_count: int,
    iteration_count: int,
    max_tool_turns: int = DEFAULT_MAX_TOOL_TURNS,
) -> bool:
    if tool_call_count > 0 and iteration_count < max_tool_turns:
        return True

    if finish_reason in {"stop", "length"}:
        return False

    if finish_reason in {"tool_calls", "function_call"}:
        return tool_call_count > 0 and iteration_count < max_tool_turns

    if tool_call_count <= 0:
        return False

    return iteration_count < max_tool_turns


def _record_timing(trace_logger: RequestTraceLogger | None, stage: str, elapsed_seconds: float) -> None:
    if trace_logger is None:
        return

    trace_logger.record(
        "timing",
        stage=stage,
        elapsed_ms=round(elapsed_seconds * 1000, 2),
    )


def _context_budget_exceeded(
    response: CreateChatCompletionResponse,
    max_context_tokens: int,
) -> bool:
    """Return True if the accumulated prompt context exceeds the budget."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return False
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    if not isinstance(prompt_tokens, int):
        return False
    return prompt_tokens > max_context_tokens


def _format_tool_loop_stop_message(*, tool_turns_completed: int, max_tool_turns: int) -> str:
    return f"stopping tool loop after {tool_turns_completed} turn(s); max_tool_turns={max_tool_turns}"


def _extract_tool_message_text(message: ChatCompletionRequestMessage | Any) -> str | None:
    message_root = getattr(message, "root", message)
    if isinstance(message_root, dict):
        role_value = message_root.get("role")
        content = message_root.get("content")
    else:
        role_value = getattr(getattr(message_root, "role", None), "value", getattr(message_root, "role", None))
        content = getattr(message_root, "content", None)

    if role_value != "tool":
        return None

    if content is None:
        return None

    if hasattr(content, "root"):
        content = content.root

    if isinstance(content, list):
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value:
                    text_chunks.append(text_value)
            elif hasattr(item, "text"):
                text_value = getattr(item, "text", None)
                if isinstance(text_value, str) and text_value:
                    text_chunks.append(text_value)
            elif hasattr(item, "root") and hasattr(item.root, "text"):
                text_value = getattr(item.root, "text", None)
                if isinstance(text_value, str) and text_value:
                    text_chunks.append(text_value)
        if text_chunks:
            return " ".join(text_chunks[:2])

    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        text_value = content.get("text")
        if isinstance(text_value, str):
            return text_value

    return None


def _normalize_finish_reason(value: str | FinishReason1 | None) -> FinishReason1 | None:
    if value is None:
        return None

    if isinstance(value, FinishReason1):
        return value

    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    mapping = {
        "stop": FinishReason1.stop,
        "length": FinishReason1.length,
        "tool_calls": FinishReason1.tool_calls,
        "content_filter": FinishReason1.content_filter,
        "function_call": FinishReason1.function_call,
    }
    return mapping.get(normalized)


def _build_tool_error_response(response: CreateChatCompletionResponse, tool_errors: list[str]) -> CreateChatCompletionResponse:
    error_summary = "; ".join(tool_errors)
    response.choices[0].message.content = (
        "I wasn't able to complete the request because one or more MCP tool calls failed: "
        + error_summary
    )
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = _normalize_finish_reason("stop") or FinishReason1.stop
    return response


def _is_recoverable_tool_error(error: str) -> bool:
    """Return True if a tool error is recoverable by feeding it back to the model.

    Validation errors (e.g. ``sequential-thinking`` rejecting malformed
    arguments) are recoverable — the model can correct its arguments and retry.
    Fatal errors (no client found, unknown tool) are not recoverable by the
    model, so the loop should stop rather than spin.
    """
    if not error:
        return False
    lowered = error.lower()
    fatal_markers = (
        "no mcp client found",
        "unknown tool",
        "tool not found",
        "client not found",
        "not found for tool",
    )
    if any(marker in lowered for marker in fatal_markers):
        return False
    return True


def _should_stop_tool_loop_on_tool_errors(
    tool_errors: list[str],
    request_messages: list[ChatCompletionRequestMessage],
) -> bool:
    if not tool_errors:
        return False

    # Fatal errors (no client / unknown tool) cannot be fixed by the model, so
    # stop immediately regardless of evidence.
    if any(not _is_recoverable_tool_error(error) for error in tool_errors):
        return True

    # Timeout errors with partial evidence are recoverable — the model can
    # continue with what it has. Keep looping even if there are several.
    evidence_text = "\n".join(
        _extract_tool_message_text(message) or ""
        for message in request_messages
        if getattr(getattr(message, "root", message), "role", None) == "tool"
    )
    timeout_error_count = sum(1 for error in tool_errors if "timeout" in error.lower() or "timed out" in error.lower())
    if timeout_error_count and evidence_text.strip():
        return False

    # Recoverable errors (validation, timeout) should be fed back to the model
    # for correction. Only stop if there are too many to avoid an infinite loop.
    if len(tool_errors) > 3:
        return True

    return False


def _build_synthesis_request(
    request: CreateChatCompletionRequest,
    *,
    stop_reason: str,
    request_messages: list[ChatCompletionRequestMessage],
) -> CreateChatCompletionRequest:
    synthesis_request = request.model_copy(deep=True)
    synthesis_request.messages = list(request_messages)
    synthesis_request.tools = []

    instruction = (
        "Synthesize the information gathered from the tool results into a helpful final answer. "
        "Use the evidence already present in the conversation, be concise but complete, and "
        "avoid mentioning the tool-loop limit unless it is necessary to explain missing information."
    )
    if stop_reason == "max_tool_turns":
        instruction += " The tool workflow stopped early, so if some information is incomplete, say so clearly."
    elif stop_reason == "max_context_tokens":
        instruction += " The tool workflow stopped early because the conversation context grew too large, so if some information is incomplete, say so clearly."

    synthesis_request.messages.append(
        ChatCompletionRequestMessage.model_validate(
            {
                "role": "user",
                "content": instruction,
            }
        )
    )
    return synthesis_request


async def _try_synthesize_tool_loop_result(
    client: Any,
    request: CreateChatCompletionRequest,
    *,
    stop_reason: str,
    request_messages: list[ChatCompletionRequestMessage],
) -> CreateChatCompletionResponse | None:
    synthesis_request = _build_synthesis_request(
        request,
        stop_reason=stop_reason,
        request_messages=request_messages,
    )
    try:
        text = (
            await client.post(
                "/chat/completions",
                json=synthesis_request.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
            )
        ).text
        response = CreateChatCompletionResponse.model_validate_json(text)
        if response.choices and getattr(response.choices[0].message, "content", None) is not None:
            response.choices[0].message.tool_calls = None
            response.choices[0].finish_reason = _normalize_finish_reason("stop") or FinishReason1.stop
            return response
    except Exception as exc:
        logger.warning(f"tool loop synthesis request failed: {exc}")

    return None


def _extract_message_text(message: ChatCompletionRequestMessage | Any) -> str:
    message_root = getattr(message, "root", message)
    if isinstance(message_root, dict):
        content = message_root.get("content")
    else:
        content = getattr(message_root, "content", None)

    if content is None:
        return ""

    if hasattr(content, "root"):
        content = content.root

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value:
                    text_chunks.append(text_value)
            elif hasattr(item, "text"):
                text_value = getattr(item, "text", None)
                if isinstance(text_value, str) and text_value:
                    text_chunks.append(text_value)
            elif hasattr(item, "root") and hasattr(item.root, "text"):
                text_value = getattr(item.root, "text", None)
                if isinstance(text_value, str) and text_value:
                    text_chunks.append(text_value)
        return " ".join(text_chunks)

    if isinstance(content, dict):
        text_value = content.get("text")
        if isinstance(text_value, str):
            return text_value

    if hasattr(content, "text"):
        text_value = getattr(content, "text", None)
        if isinstance(text_value, str):
            return text_value

    return ""


def _extract_tool_calls(message: ChatCompletionRequestMessage | Any) -> list[Any]:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls is None:
        return []

    if hasattr(tool_calls, "root"):
        tool_calls = tool_calls.root

    if isinstance(tool_calls, list):
        return tool_calls

    if isinstance(tool_calls, tuple):
        return list(tool_calls)

    if isinstance(tool_calls, dict):
        return [tool_calls]

    return []


def _should_use_empty_content_fallback(message: ChatCompletionRequestMessage, finish_reason: str | None) -> bool:
    if finish_reason in {"tool_calls", "function_call"}:
        return False

    if _extract_tool_calls(message):
        return False

    return _extract_message_text(message).strip() == ""


def _build_empty_content_response(
    response: CreateChatCompletionResponse,
    *,
    request_messages: list[ChatCompletionRequestMessage],
    stop_reason: str,
) -> CreateChatCompletionResponse:
    summary_parts: list[str] = []
    if stop_reason == "empty_response":
        summary_parts.append("The model returned an empty completion, so I synthesized the available tool evidence into a concise answer.")
    else:
        summary_parts.append("The model returned an empty completion, so I synthesized the available context into a concise answer.")

    if request_messages:
        tool_messages = []
        for message in request_messages:
            tool_text = _extract_tool_message_text(message)
            if tool_text:
                tool_messages.append(tool_text)

        if tool_messages:
            informative_messages = [
                message for message in tool_messages if message and not _looks_like_empty_search_fallback(message)
            ]
            if informative_messages:
                compact_summary = _summarize_tool_messages(informative_messages)
                summary_parts.append(_format_tool_synthesis(compact_summary, stop_reason, tool_messages))
            else:
                summary_parts.append(_format_weak_evidence_fallback(stop_reason))

    content = "\n\n".join(summary_parts)
    if content:
        content = content.strip()
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = _normalize_finish_reason("stop") or FinishReason1.stop
    return response


def _build_tool_loop_stop_response(
    response: CreateChatCompletionResponse,
    *,
    stop_reason: str,
    request_messages: list[ChatCompletionRequestMessage],
) -> CreateChatCompletionResponse:
    summary_parts: list[str] = []
    if stop_reason == "max_tool_turns":
        summary_parts.append("Note: The search or tool workflow reached its turn limit before finishing.")
    elif stop_reason == "max_context_tokens":
        summary_parts.append("Note: The search or tool workflow stopped early because the conversation context grew too large.")
    else:
        summary_parts.append("Note: The workflow stopped before it could finish.")

    if request_messages:
        tool_messages = []
        for message in request_messages:
            tool_text = _extract_tool_message_text(message)
            if tool_text:
                tool_messages.append(tool_text)

        if tool_messages:
            informative_messages = [
                message
                for message in tool_messages
                if message and not _looks_like_empty_search_fallback(message)
            ]
            if informative_messages:
                compact_summary = _summarize_tool_messages(informative_messages)
                summary_parts.append(_format_tool_synthesis(compact_summary, stop_reason, tool_messages))
            else:
                summary_parts.append(_format_weak_evidence_fallback(stop_reason))

    content = "\n\n".join(summary_parts)
    if content:
        content = content.strip()
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = _normalize_finish_reason("stop") or FinishReason1.stop
    return response


def _looks_like_empty_search_fallback(message: str) -> bool:
    lowered = message.lower()
    fallback_markers = (
        "no results were found",
        "bot detection",
        "try rephrasing your search",
        "try again in a few minutes",
        "returned no matches",
    )
    return any(marker in lowered for marker in fallback_markers)


def _has_only_weak_tool_evidence(request_messages: list[ChatCompletionRequestMessage]) -> bool:
    tool_messages = []
    for message in request_messages:
        tool_text = _extract_tool_message_text(message)
        if tool_text:
            tool_messages.append(tool_text)

    if not tool_messages:
        return False

    informative_messages = [
        message
        for message in tool_messages
        if message and not _looks_like_empty_search_fallback(message)
    ]
    if informative_messages:
        return False

    return True


def _summarize_tool_messages(messages: list[str]) -> str:
    if not messages:
        return ""

    cleaned_messages = []
    for message in messages:
        cleaned = _clean_tool_message(message)
        if cleaned:
            cleaned_messages.append(cleaned)

    if not cleaned_messages:
        return ""

    if len(cleaned_messages) == 1:
        return _summarize_message_content(cleaned_messages[0])

    unique_messages = []
    seen: set[str] = set()
    for message in cleaned_messages:
        normalized = " ".join(message.split())
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_messages.append(normalized)

    if len(unique_messages) == 1:
        return _summarize_message_content(unique_messages[0])

    filtered_messages = []
    for message in unique_messages:
        if _is_trivial_summary_fragment(message):
            continue
        filtered_messages.append(message)

    if not filtered_messages:
        return _summarize_message_content(unique_messages[0])

    if len(filtered_messages) == 1:
        return _summarize_message_content(filtered_messages[0])

    if len(filtered_messages) == 2:
        return _summarize_message_content(f"{filtered_messages[0]} Also, {filtered_messages[1]}")

    if len(filtered_messages) <= 3:
        return _summarize_message_content("; ".join(filtered_messages))

    return _summarize_message_content("; ".join(filtered_messages[:3]) + " …")


def _summarize_message_content(text: str, *, max_chars: int = 320) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""

    if "search results" in cleaned.lower():
        return _extract_search_result_titles(cleaned)

    if len(cleaned) <= max_chars:
        return cleaned

    return cleaned[: max_chars - 1].rstrip() + "…"


def _extract_search_result_titles(text: str, *, max_items: int = 3) -> str:
    normalized = " ".join(text.split())
    titles: list[str] = []
    seen_titles: set[str] = set()

    for match in re.finditer(r"(?<!\w)(\d+)\.\s+(.+?)(?=(?:\s+\d+\.\s+)|\s+URL:|\s+Summary:|$)", normalized):
        title = match.group(2).strip()
        title = re.sub(r"\s+URL:.*$", "", title)
        title = re.sub(r"\s+Summary:.*$", "", title)
        title = re.sub(r"\s{2,}", " ", title)
        title = title.strip(" -")
        if not title:
            continue
        normalized_title = " ".join(title.split())
        if normalized_title.lower() in seen_titles:
            continue
        seen_titles.add(normalized_title.lower())
        titles.append(normalized_title)
        if len(titles) >= max_items:
            break

    if titles:
        return "Top findings: " + "; ".join(titles)

    return _compact_text(text)


def _compact_text(text: str, *, max_chars: int = 320) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned

    return cleaned[: max_chars - 1].rstrip() + "…"


def _clean_tool_message(message: str) -> str:
    cleaned = " ".join(message.split())
    prefixes = (
        "result:",
        "results:",
        "note:",
        "summary:",
        "findings:",
    )
    lowered = cleaned.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break

    if cleaned.startswith("{") and cleaned.endswith("}"):
        return "structured data returned by a tool"

    if "\n" in cleaned:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(lines) > 1:
            cleaned = "; ".join(lines[:3])

    return cleaned


def _is_trivial_summary_fragment(message: str) -> bool:
    lowered = message.lower().strip()
    trivial_phrases = (
        "the tool call result is empty",
        "the tool result is empty",
        "empty",
        "no additional details",
        "no further details",
    )
    return lowered in trivial_phrases or lowered.startswith("result:") or lowered.startswith("results:")


def _format_tool_synthesis(summary: str, stop_reason: str, tool_messages: list[str]) -> str:
    is_search_like = any(_looks_like_search_result(message) for message in tool_messages)
    if is_search_like:
        title = "Search results gathered"
        if stop_reason == "max_tool_turns":
            intro = "I found some search results, but the workflow reached its turn limit before I could fully synthesize them."
        else:
            intro = "I collected some search results before stopping."
    else:
        title = "Useful information gathered"
        if stop_reason == "max_tool_turns":
            intro = "I found several relevant leads, but the workflow reached its turn limit before I could fully synthesize them."
        else:
            intro = "I collected some information before stopping."

    bullets = [
        intro,
        "",
        f"**{title}**",
        "- " + summary.replace("\n", "\n- ") if summary else "- No additional details were gathered.",
    ]
    return "\n".join(bullets)


def _format_weak_evidence_fallback(stop_reason: str) -> str:
    if stop_reason == "max_tool_turns":
        return "**No reliable evidence gathered**\n\nI wasn't able to gather enough reliable evidence before the workflow reached its turn limit. A narrower or more specific query may produce better results."

    return "**No reliable evidence gathered**\n\nI wasn't able to gather enough reliable evidence before stopping. A narrower or more specific query may produce better results."


def _looks_like_search_result(message: str) -> bool:
    lowered = message.lower()
    search_markers = (
        "repository:",
        "search results",
        "result:",
        "results:",
        "repo",
        "github",
        "mcp server",
        "open source",
    )
    return any(marker in lowered for marker in search_markers)


_PSEUDO_TOOL_CALL_PATTERN = re.compile(
    r"<\|?(?:tool_call|function_call|function)[=>|_]|<invoke\b",
    re.IGNORECASE,
)


def _contains_pseudo_tool_call_markers(text: str) -> bool:
    """Return True if the model emitted tool calls as plain text instead of
    structured ``tool_calls``.

    Some reasoning models (e.g. Liquid LFM2.5) advertise ``tools`` support but do
    not emit OpenAI-style ``tool_calls``. They instead write markers such as
    ``<|tool_call_start|>``, ``<tool_call>``, ``<function=...>``, or Anthropic-style
    ``<invoke name="...">`` directly into the assistant ``content``. The bridge
    cannot execute these, so we detect and reject them.
    """
    if not text:
        return False
    return bool(_PSEUDO_TOOL_CALL_PATTERN.search(text))


# Matches an Anthropic-style tool invocation block:
#   <invoke name="tool_name">
#     <parameter name="arg_name">value</parameter>
#   </invoke>
_INVOKE_BLOCK_PATTERN = re.compile(
    r"<invoke\s+name=[\"']([^\"']+)[\"']>(.*?)</invoke>",
    re.IGNORECASE | re.DOTALL,
)
_PARAMETER_PATTERN = re.compile(
    r"<parameter\s+name=[\"']([^\"']+)[\"']>(.*?)</parameter>",
    re.IGNORECASE | re.DOTALL,
)

# Matches a function-call block in the format some models emit:
#   <tool_call>
#     <function=fetch>
#       <parameter=max_length>3000</parameter>
#       <parameter=url>https://...</parameter>
#     </function>
#   </tool_call>
_FUNCTION_BLOCK_PATTERN = re.compile(
    r"<function=([^>]+)>(.*?)</function>",
    re.IGNORECASE | re.DOTALL,
)
_FUNCTION_PARAMETER_PATTERN = re.compile(
    r"<parameter=([^>]+)>(.*?)</parameter>",
    re.IGNORECASE | re.DOTALL,
)


def _coerce_pseudo_value(value: str) -> Any:
    """Interpret a pseudo tool-call parameter value as JSON when possible."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


def _parse_pseudo_tool_calls(text: str) -> list[tuple[str, str]]:
    """Parse pseudo tool-call markers from plain text into
    ``(tool_name, arguments_json)`` tuples.

    Supports two common formats:
    * Anthropic-style ``<invoke name="..."><parameter name="...">...</parameter></invoke>``
    * ``<tool_call><function=NAME><parameter=ARG>value</parameter></function></tool_call>``

    Returns an empty list if no parseable invocations are found.
    """
    if not text:
        return []

    calls: list[tuple[str, str]] = []

    # Format 1: <invoke name="...">...</invoke>
    for match in _INVOKE_BLOCK_PATTERN.finditer(text):
        tool_name = match.group(1).strip()
        body = match.group(2)
        if not tool_name:
            continue

        arguments: dict[str, Any] = {}
        for param in _PARAMETER_PATTERN.finditer(body):
            arg_name = param.group(1).strip()
            arg_value = param.group(2).strip()
            if not arg_name:
                continue
            arguments[arg_name] = _coerce_pseudo_value(arg_value)

        calls.append((tool_name, json.dumps(arguments)))

    # Format 2: <function=NAME>...</function>
    for match in _FUNCTION_BLOCK_PATTERN.finditer(text):
        tool_name = match.group(1).strip()
        body = match.group(2)
        if not tool_name:
            continue

        arguments: dict[str, Any] = {}
        for param in _FUNCTION_PARAMETER_PATTERN.finditer(body):
            arg_name = param.group(1).strip()
            arg_value = param.group(2).strip()
            if not arg_name:
                continue
            arguments[arg_name] = _coerce_pseudo_value(arg_value)

        calls.append((tool_name, json.dumps(arguments)))

    return calls


def _build_synthetic_tool_calls(
    parsed_calls: list[tuple[str, str]],
) -> list[Any]:
    """Build synthetic OpenAI-style tool_call objects from parsed pseudo calls.

    Each returned object has ``id``, ``type``, and ``function`` (with ``name``
    and ``arguments``) attributes, matching the shape the tool loop expects.
    """
    from types import SimpleNamespace

    tool_calls: list[Any] = []
    for index, (name, arguments_json) in enumerate(parsed_calls):
        tool_calls.append(
            SimpleNamespace(
                id=f"pseudo-call-{index}",
                type="function",
                function=SimpleNamespace(
                    name=name,
                    arguments=arguments_json,
                ),
            )
        )
    return tool_calls


async def chat_completions(
    request: CreateChatCompletionRequest,
    http_request: Request,
    trace_logger: RequestTraceLogger | None = None,
) -> CreateChatCompletionResponse:
    """performs a chat completion using the inference server"""

    if not getattr(request, "tools", None):
        request = await chat_completion_add_tools(request)
    if trace_logger is not None:
        trace_logger.record("tools_discovered", tools=[tool.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) for tool in request.tools])

    max_tool_turns = get_max_tool_turns()
    tool_timeout_seconds = get_tool_timeout_seconds()
    tool_turns_completed = 0
    tool_client_cache: dict[str, Any] = {}

    async with get_client(http_request) as client:
        while True:
            start_time = time.perf_counter()
            # logger.debug(request.model_dump_json())
            upstream_response = await client.post(
                "/chat/completions",
                #content=request.model_dump_json(
                #    exclude_defaults=True, exclude_none=True, exclude_unset=True
                #),
                json=request.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
            )
            text = upstream_response.text
            logger.debug(f"upstream chat completion response received: status={upstream_response.status_code}")
            _record_timing(trace_logger, "upstream_llm_request", time.perf_counter() - start_time)

            if upstream_response.status_code >= 400:
                logger.error(f"upstream inference server returned status {upstream_response.status_code}: {text[:2000]}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Upstream inference server returned status {upstream_response.status_code}",
                )

            try:
                response = CreateChatCompletionResponse.model_validate_json(text)
                if trace_logger is not None:
                    trace_logger.record(
                        "llm_response",
                        response=response.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                    )

                if logger.level("DEBUG").name == "DEBUG":
                    response_preview = response.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True)
                    compact_preview = json.dumps(response_preview, ensure_ascii=False)[:4000]
                    logger.debug(f"upstream response preview: {compact_preview}")
                    if response.choices:
                        message = response.choices[0].message
                        logger.debug(
                            "upstream message summary: "
                            f"role={getattr(getattr(message, 'root', message), 'role', None)}; "
                            f"content_len={len(_extract_message_text(message))}; "
                            f"tool_call_count={len(_extract_tool_calls(message))}; "
                            f"finish_reason={getattr(response.choices[0].finish_reason, 'value', None)}"
                        )
            except HTTPException:
                raise
            except Exception as e:
                logger.error("error parsing upstream chat completion response")
                logger.error(e)
                raise HTTPException(
                    status_code=502,
                    detail="Failed to parse upstream chat completion response",
                ) from e

            if not response.choices:
                logger.error("upstream chat completion response contained no choices")
                raise HTTPException(
                    status_code=502,
                    detail="Upstream chat completion response contained no choices",
                )

            msg = response.choices[0].message
            if _should_use_empty_content_fallback(msg, finish_reason_value := response.choices[0].finish_reason.value if response.choices[0].finish_reason is not None else None):
                logger.warning("upstream model returned empty assistant content without tool calls; synthesizing a fallback response from tool evidence")
                return _build_empty_content_response(
                    response,
                    request_messages=request.messages,
                    stop_reason="empty_response",
                )

            msg = ChatCompletionRequestMessage(
                role="assistant",
                content=msg.content,
                tool_calls=msg.tool_calls,
            )  # type: ignore
            request.messages.append(msg)

            finish_reason_label = response.choices[0].finish_reason.value if response.choices[0].finish_reason is not None else None
            logger.debug(
                "chat completion finish reason: "
                f"{finish_reason_label}; tool_calls={bool(getattr(response.choices[0].message, 'tool_calls', None))}"
            )
            if trace_logger is not None:
                trace_logger.record(
                    "assistant_message",
                    message=msg.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                )

            finish_reason_value = (
                response.choices[0].finish_reason.value
                if response.choices[0].finish_reason is not None
                else None
            )
            if finish_reason_value in ["stop", "length"]:
                assistant_text = _extract_message_text(msg)
                if _contains_pseudo_tool_call_markers(assistant_text):
                    # The model emitted Anthropic-style tool-call markers as
                    # plain text instead of structured tool_calls. Parse them
                    # into real tool calls and execute them, rather than
                    # failing the request.
                    parsed_calls = _parse_pseudo_tool_calls(assistant_text)
                    if parsed_calls:
                        logger.warning(
                            f"model emitted pseudo tool-call markers as plain text; "
                            f"parsing {len(parsed_calls)} tool call(s) for execution"
                        )
                        synthetic_calls = _build_synthetic_tool_calls(parsed_calls)
                        msg.tool_calls = synthetic_calls
                        # The tool loop reads tool_calls from
                        # response.choices[0].message, so mirror them there too.
                        response.choices[0].message.tool_calls = synthetic_calls
                        if trace_logger is not None:
                            trace_logger.record(
                                "pseudo_tool_calls_parsed",
                                tool_calls=[{"name": name, "arguments": arguments} for name, arguments in parsed_calls],
                            )
                        # Fall through to the normal tool-call execution path.
                    else:
                        logger.warning(
                            "model emitted pseudo tool-call markers as plain text "
                            "(e.g. <|tool_call_start|> / <tool_call> / <function=...>); "
                            "model does not support structured tool calls via the bridge"
                        )
                        raise HTTPException(
                            status_code=502,
                            detail=(
                                "The model does not support structured tool calls and "
                                "emitted tool-call markers as plain text. Use a model "
                                "with native tool-call support."
                            ),
                        )
                else:
                    logger.debug("no tool calls found")
                    return response

            logger.debug("tool calls found")
            if trace_logger is not None:
                trace_logger.record("tool_call_decision", finish_reason=finish_reason_value)
            tool_call_items = []
            for tool_call in _extract_tool_calls(response.choices[0].message):
                function = getattr(tool_call, "function", None)
                if isinstance(function, dict):
                    name = function.get("name")
                    arguments = function.get("arguments")
                else:
                    name = getattr(function, "name", None)
                    arguments = getattr(function, "arguments", None)

                if name is None:
                    continue

                tool_call_items.append((name, arguments))

            if not tool_call_items:
                # The model may have advertised a tool_calls finish reason but
                # emitted Anthropic-style pseudo markers as plain text content
                # with no structured tool_calls. Parse them so the tools still
                # execute instead of returning the raw markers.
                assistant_text = _extract_message_text(msg)
                if _contains_pseudo_tool_call_markers(assistant_text):
                    parsed_calls = _parse_pseudo_tool_calls(assistant_text)
                    if parsed_calls:
                        logger.warning(
                            f"model returned tool_calls finish reason with pseudo markers; "
                            f"parsing {len(parsed_calls)} tool call(s) for execution"
                        )
                        synthetic_calls = _build_synthetic_tool_calls(parsed_calls)
                        msg.tool_calls = synthetic_calls
                        response.choices[0].message.tool_calls = synthetic_calls
                        if trace_logger is not None:
                            trace_logger.record(
                                "pseudo_tool_calls_parsed",
                                tool_calls=[{"name": name, "arguments": arguments} for name, arguments in parsed_calls],
                            )
                        tool_call_items = [(name, arguments) for name, arguments in parsed_calls]
                    else:
                        logger.warning("model returned a tool-like finish reason without tool calls; stopping loop")
                        return response
                else:
                    logger.warning("model returned a tool-like finish reason without tool calls; stopping loop")
                    return response

            if not should_continue_tool_loop(
                finish_reason_value,
                tool_call_count=len(tool_call_items),
                iteration_count=tool_turns_completed,
                max_tool_turns=max_tool_turns,
            ):
                logger.warning(
                    _format_tool_loop_stop_message(
                        tool_turns_completed=tool_turns_completed,
                        max_tool_turns=max_tool_turns,
                    )
                )
                synthesized_response = await _try_synthesize_tool_loop_result(
                    client,
                    request,
                    stop_reason="max_tool_turns",
                    request_messages=request.messages,
                )
                if synthesized_response is not None:
                    return synthesized_response
                return _build_tool_loop_stop_response(
                    response,
                    stop_reason="max_tool_turns",
                    request_messages=request.messages,
                )

            if _has_only_weak_tool_evidence(request.messages):
                logger.warning("tool evidence is weak or empty; stopping tool loop before another iteration")
                synthesized_response = await _try_synthesize_tool_loop_result(
                    client,
                    request,
                    stop_reason="max_tool_turns",
                    request_messages=request.messages,
                )
                if synthesized_response is not None:
                    return synthesized_response
                return _build_tool_loop_stop_response(
                    response,
                    stop_reason="max_tool_turns",
                    request_messages=request.messages,
                )

            max_context_tokens = get_max_context_tokens()
            if _context_budget_exceeded(response, max_context_tokens):
                prompt_tokens = getattr(getattr(response, "usage", None), "prompt_tokens", None)
                logger.warning(
                    f"tool loop context budget exceeded ({prompt_tokens} > {max_context_tokens} tokens); "
                    "stopping tool loop and synthesizing a final answer"
                )
                synthesized_response = await _try_synthesize_tool_loop_result(
                    client,
                    request,
                    stop_reason="max_context_tokens",
                    request_messages=request.messages,
                )
                if synthesized_response is not None:
                    return synthesized_response
                return _build_tool_loop_stop_response(
                    response,
                    stop_reason="max_context_tokens",
                    request_messages=request.messages,
                )

            tool_turns_completed += 1

            if tool_call_items:
                tool_call_results = await call_tools(
                    tool_call_items,
                    timeout=tool_timeout_seconds,
                    trace_logger=trace_logger,
                    client_cache=tool_client_cache,
                )
                if trace_logger is not None:
                    trace_logger.record("mcp_tool_calls", tool_calls=[{"name": name, "arguments": arguments} for name, arguments in tool_call_items])

                tool_errors: list[str] = []
                tool_call_messages = _extract_tool_calls(response.choices[0].message)
                for tool_call, tool_call_result in zip(
                    tool_call_messages,
                    tool_call_results,
                ):
                    function = getattr(tool_call, "function", None)
                    if isinstance(function, dict):
                        tool_name = function.get("name", "unknown")
                    else:
                        tool_name = getattr(function, "name", "unknown")

                    if tool_call_result is None:
                        logger.warning(
                            f"tool call '{tool_name}' returned no result"
                        )
                        continue

                    logger.debug(
                        "tool call completed: "
                        f"name={tool_name}; "
                        f"parts={len(getattr(tool_call_result, 'content', []) or [])}; "
                        f"isError={getattr(tool_call_result, 'isError', False)}"
                    )
                    if trace_logger is not None:
                        trace_logger.record(
                            "mcp_tool_result",
                            tool_name=tool_name,
                            result=tool_call_result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                        )

                    if getattr(tool_call_result, "content", None):
                        preview_text = str(tool_call_result.content)
                        preview_text = " ".join(preview_text.split())
                        if len(preview_text) > 400:
                            preview_text = preview_text[:397].rstrip() + "…"
                        logger.debug(
                            "tool call result content preview: "
                            f"{preview_text}"
                        )

                    if getattr(tool_call_result, "isError", False):
                        error_text = next(
                            (
                                part.text
                                for part in getattr(tool_call_result, "content", [])
                                if getattr(part, "type", None) == "text"
                            ),
                            "tool call failed",
                        )
                        tool_errors.append(f"{tool_name}: {error_text}")

                    tools_content = sanitize_tool_result_content(
                        tool_name,
                        tool_call_result,
                    )
                    request.messages.append(
                        ChatCompletionRequestMessage.model_validate(
                            {
                                "role": "tool",
                                "content": tools_content,
                                "tool_call_id": getattr(tool_call, "id", None) if not isinstance(tool_call, dict) else tool_call.get("id"),
                            }
                        )
                    )

                    if trace_logger is not None:
                        trace_logger.record(
                            "tool_message",
                            tool_name=tool_call.function.name,
                            tool_result=tool_call_result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                        )

                    logger.debug("sending next iteration of chat completion request")

                if tool_errors:
                    should_stop = _should_stop_tool_loop_on_tool_errors(tool_errors, request.messages)
                    if should_stop:
                        logger.warning(
                            f"tool call failures detected; stopping tool loop: {'; '.join(tool_errors)}"
                        )
                        return _build_tool_error_response(response, tool_errors)

                    # Recoverable failures (e.g. a validation error on one tool
                    # call). The error messages were already appended to
                    # request.messages above, so continuing the loop lets the
                    # LLM see exactly what went wrong and correct its arguments
                    # on the next iteration. The loop is still bounded by
                    # max_tool_turns via should_continue_tool_loop.
                    logger.warning(
                        f"tool call failures detected; feeding errors back to the model for correction: {'; '.join(tool_errors)}"
                    )
                    continue
