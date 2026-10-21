import json
import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from mcp_bridge.config import config

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("mcp_bridge")


class _TracingState:
    initialized = False
    provider: TracerProvider | None = None


def payload_preview(value: Any, limit: int = 1600) -> str:
    """Return a redacted payload preview, or ``""`` when capture is off.

    PII-safe by default: request bodies, responses and tool arguments can contain
    user data, so they only reach span attributes when the operator opts in with
    ``telemetry.capture_payloads``. When enabled, ``redact_sensitive_data`` is
    applied first.
    """
    if not getattr(config.telemetry, "capture_payloads", False):
        return ""
    try:
        from mcp_bridge.logging import redact_sensitive_data

        redacted = redact_sensitive_data(value)
        if isinstance(redacted, str):
            text = redacted
        else:
            text = json.dumps(redacted, ensure_ascii=False, default=str)
        return text[:limit]
    except Exception:
        return ""


def setup_tracing(app) -> None:
    if getattr(app.state, "_tracing_initialized", False):
        return

    app.state._tracing_initialized = True

    try:
        resource = Resource(attributes={"service.name": config.telemetry.service_name})
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)
        _TracingState.provider = provider

        if config.telemetry.enabled:
            try:
                otlp_exporter = OTLPSpanExporter(endpoint=config.telemetry.otel_endpoint)
                span_processor = BatchSpanProcessor(otlp_exporter)
                provider.add_span_processor(span_processor)
                logger.info(
                    "Telemetry enabled for %s via %s",
                    config.telemetry.service_name,
                    config.telemetry.otel_endpoint,
                )
            except Exception as exc:
                logger.warning(
                    "Telemetry export could not be initialized for %s: %s; continuing without export",
                    config.telemetry.otel_endpoint,
                    exc,
                )

        FastAPIInstrumentor().instrument_app(app)
        HTTPXClientInstrumentor().instrument()
    except Exception:
        app.state._tracing_initialized = False
        logger.exception("Tracing initialization failed")
        raise


def shutdown_tracing() -> None:
    """Flush and stop the tracer provider exactly once.

    ``TracerProvider.shutdown()`` is synchronous and shuts down the batch span
    processor, so the caller should run it off the event loop. Idempotent: the
    provider is cleared before shutting it down.
    """
    provider = _TracingState.provider
    _TracingState.provider = None
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception:
        logger.exception("Tracing shutdown failed")
