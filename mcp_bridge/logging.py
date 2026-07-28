import json
import sys
from collections.abc import Mapping
from typing import Any

from loguru import logger

SENSITIVE_KEYWORDS = ("key", "token", "secret", "password", "authorization")


def redact_sensitive_data(value: Any) -> Any:
    """Recursively redact secrets from dictionaries and lists."""

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(keyword in str(key).lower() for keyword in SENSITIVE_KEYWORDS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_sensitive_data(item)
        return redacted

    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]

    return value


def configure_logging(level: str = "INFO") -> None:
    """Configure loguru to emit structured JSON logs to stderr."""

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        enqueue=True,
        serialize=True,
        backtrace=False,
        diagnose=False,
    )


def log_event(message: str, **fields: Any) -> None:
    """Emit a structured event log entry while redacting secrets."""

    logger.info(json.dumps(redact_sensitive_data({"message": message, **fields})))
