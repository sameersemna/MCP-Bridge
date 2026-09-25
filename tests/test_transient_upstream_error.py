"""Regression tests for the transient-upstream-error classifier.

The classifier decides whether an HTTP 200 body from the inference server is a
*transient provider error* worth retrying, or a normal completion to be used
as-is.

It previously lowercased the ENTIRE response body and substring-matched generic
English words (``timeout``, ``rate limit``, ``unavailable``, ``temporarily``,
...). Any model that merely *wrote about* those words was misclassified as an
upstream error: its response was retried three times and then discarded,
turning a good answer into a degraded fallback. The classifier now only treats
a body as an error when it is an actual error envelope and never inspects a
completion's assistant content.
"""

import json

from mcp_bridge.openai_clients.chatCompletion import (
    _is_retryable_upstream_status,
    _is_transient_upstream_error,
)


def _completion(content: str, finish_reason: str = "stop") -> str:
    return json.dumps(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor/model:free",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                }
            ],
        }
    )


# --- The regression: a valid completion must never be retryable -------------


def test_valid_completion_mentioning_rate_limit_is_not_transient():
    body = _completion(
        "A proxy can handle rate limiting, caching, or multi-tenant architectures."
    )

    assert _is_transient_upstream_error(body) is False
    assert _is_retryable_upstream_status(200, body) is False


def test_valid_completion_mentioning_timeout_is_not_transient():
    body = _completion("I set a timeout of 30 seconds in the configuration.")

    assert _is_transient_upstream_error(body) is False
    assert _is_retryable_upstream_status(200, body) is False


def test_valid_completion_mentioning_every_marker_is_not_transient():
    body = _completion(
        "The service was temporarily unavailable and returned a rate limit / "
        "too many requests, then an internal server error / bad gateway / "
        "service unavailable, and finally a timeout / timed out / timeout occurred."
    )

    assert _is_transient_upstream_error(body) is False


def test_valid_tool_call_completion_is_not_transient():
    body = json.dumps(
        {
            "id": "gen-2",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor/model:free",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "search", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
        }
    )

    assert _is_transient_upstream_error(body) is False


# --- Real error envelopes are still retryable -------------------------------


def test_error_envelope_with_timeout_message_is_transient():
    body = json.dumps({"error": {"message": "A Timeout Occurred", "code": 504}})

    assert _is_transient_upstream_error(body) is True
    assert _is_retryable_upstream_status(200, body) is True


def test_error_envelope_with_provider_error_message_is_transient():
    body = json.dumps({"error": {"message": "Upstream provider error", "code": 502}})

    assert _is_transient_upstream_error(body) is True


def test_non_json_gateway_error_body_is_transient():
    assert _is_transient_upstream_error("502 Bad Gateway") is True
    assert _is_transient_upstream_error("upstream timeout") is True


def test_json_without_choices_is_transient_when_it_mentions_a_marker():
    body = json.dumps({"message": "the service is temporarily down"})

    assert _is_transient_upstream_error(body) is True


def test_json_with_empty_choices_list_is_transient_when_it_mentions_a_marker():
    body = json.dumps({"choices": [], "note": "rate limit exceeded"})

    assert _is_transient_upstream_error(body) is True


def test_error_key_takes_precedence_even_when_choices_are_present():
    body = json.dumps(
        {
            "error": {"message": "provider returned error", "code": 502},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
        }
    )

    assert _is_transient_upstream_error(body) is True


# --- Envelope-only: assistant text must not leak into the decision ----------


def test_error_envelope_without_markers_is_not_transient():
    body = json.dumps({"error": {"message": "invalid api key", "code": 401}})

    assert _is_transient_upstream_error(body) is False


def test_markers_inside_assistant_content_of_an_error_envelope_are_ignored():
    # The error message itself is not transient; the transient wording lives
    # only in the (irrelevant) assistant content, so this must NOT retry.
    body = json.dumps(
        {
            "error": {"message": "bad request", "code": 400},
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "the provider was temporarily unavailable, rate limit",
                    },
                }
            ],
        }
    )

    assert _is_transient_upstream_error(body) is False


# --- Status-code handling ---------------------------------------------------


def test_rate_limit_status_is_always_retryable():
    assert _is_retryable_upstream_status(429, "") is True


def test_server_error_status_is_always_retryable():
    assert _is_retryable_upstream_status(500, "") is True
    assert _is_retryable_upstream_status(503, "") is True


def test_permanent_client_error_status_is_not_retryable():
    assert _is_retryable_upstream_status(400, "") is False
    assert _is_retryable_upstream_status(403, "") is False


def test_empty_body_is_not_transient():
    assert _is_transient_upstream_error("") is False
