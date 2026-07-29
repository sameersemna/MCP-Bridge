import logging

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


def setup_tracing(app) -> None:
    if getattr(app.state, "_tracing_initialized", False):
        return

    app.state._tracing_initialized = True

    try:
        resource = Resource(attributes={"service.name": config.telemetry.service_name})
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)

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
