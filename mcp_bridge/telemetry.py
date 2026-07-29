import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from mcp_bridge.config import config


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
            otlp_exporter = OTLPSpanExporter(endpoint=config.telemetry.otel_endpoint)
            span_processor = BatchSpanProcessor(otlp_exporter)
            provider.add_span_processor(span_processor)

        FastAPIInstrumentor().instrument_app(app)
        HTTPXClientInstrumentor().instrument()
    except Exception:
        app.state._tracing_initialized = False
        raise
