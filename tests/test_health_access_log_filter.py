"""Tests for suppressing the HEALTHCHECK's `/health` access-log lines.

The container's HEALTHCHECK polls `/health` on every interval. Each poll
produced one uvicorn access-log line, flooding the CLI log stream. The filter
drops *only* those lines: the endpoint still runs and still drives Docker's
health status, and every other request stays visible.
"""

import logging

from mcp_bridge.logging import (
    _is_quiet_access_path,
    _SuppressAccessLogPaths,
    build_uvicorn_log_config,
)


def _access_record(method: str, path: str, status: int = 200) -> logging.LogRecord:
    """Build a record shaped like uvicorn's access log.

    uvicorn calls:
        access_logger.info('%s - "%s %s HTTP/%s" %d', client, method, path, ver, status)
    so ``record.args`` is (client, method, path, version, status).
    """
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", method, path, "1.1", status),
        exc_info=None,
    )
    return record


def _keep(record: logging.LogRecord) -> bool:
    return _SuppressAccessLogPaths().filter(record)


def test_health_access_line_is_suppressed():
    assert _keep(_access_record("GET", "/health")) is False


def test_health_with_trailing_slash_is_suppressed():
    assert _keep(_access_record("GET", "/health/")) is False


def test_health_with_query_string_is_suppressed():
    assert _keep(_access_record("GET", "/health?verbose=1")) is False


def test_health_failure_status_is_still_suppressed():
    # A failing healthcheck is reported by `docker ps`/the healthcheck result,
    # so the access line stays suppressed even at 500.
    assert _keep(_access_record("GET", "/health", status=500)) is False


def test_other_request_lines_are_kept():
    assert _keep(_access_record("POST", "/v1/chat/completions")) is True
    assert _keep(_access_record("GET", "/v1/models")) is True


def test_path_that_merely_contains_health_is_kept():
    assert _keep(_access_record("GET", "/healthcheck")) is True
    assert _keep(_access_record("GET", "/myhealth")) is True
    assert _is_quiet_access_path("/healthz") is False


def test_non_tuple_args_are_left_alone():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="preformatted message",
        args=None,
        exc_info=None,
    )
    assert _keep(record) is True


def test_build_uvicorn_log_config_preserves_uvicorn_defaults_and_adds_filter():
    config = build_uvicorn_log_config()

    # uvicorn's own handlers/formatters survive.
    assert "handlers" in config and "access" in config["handlers"]
    assert "formatters" in config
    # The filter is registered and attached to the access handler only.
    assert "suppress_access_log_paths" in config["filters"]
    assert "suppress_access_log_paths" in config["handlers"]["access"]["filters"]
    assert "suppress_access_log_paths" not in config["handlers"]["default"].get("filters", [])


def test_build_uvicorn_log_config_does_not_mutate_uvicorn_default(monkeypatch):
    from uvicorn.config import LOGGING_CONFIG

    before = repr(LOGGING_CONFIG)
    build_uvicorn_log_config()
    assert repr(LOGGING_CONFIG) == before
