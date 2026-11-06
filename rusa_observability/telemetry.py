from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

_lock = threading.Lock()
_tracer_provider: Any | None = None
_logger_provider: Any | None = None


def configure_telemetry(service_name: str) -> Any | None:
    """Configure OTLP traces and logs only when explicitly enabled by deployment."""
    global _tracer_provider, _logger_provider
    if os.getenv("RUSA_OTEL_ENABLED", "false").lower() != "true":
        return None
    with _lock:
        if _tracer_provider is not None:
            return _tracer_provider

        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
        insecure = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true").lower() == "true"
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "rusa-ai",
                "deployment.environment": os.getenv("ENVIRONMENT", "production"),
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
        )
        trace.set_tracer_provider(tracer_provider)

        logger_provider = None
        if os.getenv("RUSA_OTEL_LOGS_ENABLED", "false").lower() == "true":
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=insecure))
            )
            logging.getLogger().addHandler(
                LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
            )

        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
        PsycopgInstrumentor().instrument(tracer_provider=tracer_provider)

        _tracer_provider = tracer_provider
        _logger_provider = logger_provider
        return tracer_provider


def instrument_fastapi(app: Any, service_name: str) -> None:
    provider = configure_telemetry(service_name)
    if provider is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)


def instrument_asgi(app: Any, service_name: str) -> Any:
    provider = configure_telemetry(service_name)
    if provider is None:
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    return OpenTelemetryMiddleware(app, tracer_provider=provider)


@contextmanager
def trace_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    tracer = trace.get_tracer("rusa-ai")
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise
