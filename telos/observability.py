"""OpenTelemetry tracing setup for Telos.

The application layer (``telos.api.app``) calls :func:`init_tracing` once
during lifespan startup, passing the OTLP endpoint and service name from
``Settings``. When the endpoint is empty, init is a no-op — spans still
work (the SDK's default proxy tracer) but nothing is exported. Suitable
for tests, local dev without Jaeger, or deployments that haven't opted
in to tracing.

Logging instrumentation attaches OTel trace fields to ``LogRecord``
objects for logging backends that include them in their formatter.

Span helpers:

- :func:`tracer` returns the module-level tracer used everywhere.
- :func:`span` is a context-manager wrapper that adds attributes from
  kwargs and records exceptions automatically.
"""

import logging
from contextlib import contextmanager
from typing import Any

from opentelemetry import _logs, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import ProxyTracerProvider, Span, Status, StatusCode

from telos.local_time import local_datetime_from_timestamp

log = logging.getLogger("telos.observability")

# Internal grouping name passed to ``trace.get_tracer``. Not user-tunable —
# OTel uses it to bucket spans by emitting library, not to label the
# service in the trace backend (that's ``service_name``).
_TRACER_NAME = "telos"


class OTelLogFormatter(logging.Formatter):
    """Formatter that adds trace context when OTel placed it on the record."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = local_datetime_from_timestamp(record.created)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")

    def format(self, record: logging.LogRecord) -> str:
        record.otel_context = _otel_log_context(record)
        return super().format(record)


def init_tracing(endpoint: str = "", service_name: str = "telos") -> bool:
    """Initialise OTel tracing once per process. Idempotent.

    Returns ``True`` when a concrete tracer provider already exists or a
    real exporter was wired. Empty-endpoint calls leave the provider
    untouched, so a later configured call can still enable tracing.
    """
    endpoint = endpoint.strip()
    service_name = service_name.strip() or "telos"
    if _tracing_provider_is_configured():
        return True

    if not endpoint:
        log.info("OTel: no OTLP endpoint configured — tracing disabled")
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    logger_provider = LoggerProvider(resource=resource)
    log_exporter = OTLPLogExporter(endpoint=endpoint)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    _logs.set_logger_provider(logger_provider)

    # Auto-instrument httpx clients (used by the Recordatio transport) so
    # traceparent propagates to Recordatio with no per-call code.
    _instrument_once(HTTPXClientInstrumentor())
    # Enrich log records with OTel's standard otelTraceID / otelSpanID fields
    # while leaving Telos' own formatter/handlers in charge of local output.
    _instrument_once(LoggingInstrumentor(), set_logging_format=False)
    _add_otel_log_handler_once(logger_provider)

    log.info("OTel: tracing/logging enabled — service=%r exporting to %s", service_name, endpoint)
    return True


def _tracing_provider_is_configured() -> bool:
    """Return whether OTel already has a concrete tracer provider."""
    return not isinstance(trace.get_tracer_provider(), ProxyTracerProvider)


def _instrument_once(instrumentor: Any, *args: Any, **kwargs: Any) -> None:
    """Call an OTel instrumentor only if it has not already patched its target."""
    if getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
        return
    instrumentor.instrument(*args, **kwargs)


def _add_otel_log_handler_once(logger_provider: LoggerProvider) -> None:
    """Attach the OTel log exporter to Python logging once per process."""
    root_logger = logging.getLogger()
    if any(getattr(handler, "_telos_otel_log_handler", False) for handler in root_logger.handlers):
        return
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    handler._telos_otel_log_handler = True
    root_logger.addHandler(handler)


def _otel_log_context(record: logging.LogRecord) -> str:
    """Return a compact log context suffix, or an empty string when absent."""
    trace_id = _useful_otel_id(getattr(record, "otelTraceID", None))
    span_id = _useful_otel_id(getattr(record, "otelSpanID", None))
    parts = []
    if trace_id:
        parts.append(f"trace_id={trace_id}")
    if span_id:
        parts.append(f"span_id={span_id}")
    return f" {' '.join(parts)}" if parts else ""


def _useful_otel_id(value: Any) -> str:
    """Normalise OTel's missing/invalid sentinels to an empty string."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or set(text) <= {"0"}:
        return ""
    return text


def tracer() -> trace.Tracer:
    """Return the module-level tracer. Safe before ``init_tracing``."""
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def span(name: str, **attrs: Any):
    """Context-manager wrapper around ``tracer().start_as_current_span``.

    - Attributes from kwargs are added with ``set_attribute`` (only
      primitive types — non-primitives are coerced to ``str``).
    - Any exception raised inside the block is recorded on the span and
      the span status is set to ``ERROR``; the exception still
      propagates.
    """
    with tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                _safe_set_attribute(sp, k, v)
        try:
            yield sp
        except Exception as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def mark_error(sp: Span, description: str, exc: Exception | None = None) -> None:
    """Mark ``sp`` as failed without requiring an exception to escape.

    Tool handlers intentionally convert many failures into model-visible
    ``ToolResult(is_error=True)`` values. Those handled failures should still
    show as error spans in trace backends.
    """
    if exc is not None:
        sp.record_exception(exc)
    sp.set_status(Status(StatusCode.ERROR, description))


def _safe_set_attribute(sp: Span, key: str, value: Any) -> None:
    """OTel only accepts primitives + lists of primitives; coerce others to str."""
    if isinstance(value, (str, bool, int, float)):
        sp.set_attribute(key, value)
    elif isinstance(value, (list, tuple)) and all(
        isinstance(v, (str, bool, int, float)) for v in value
    ):
        sp.set_attribute(key, list(value))
    else:
        sp.set_attribute(key, str(value))


def current_trace_id() -> str | None:
    """Return the active span's trace_id as a 32-char hex string, or None.

    Useful for stamping trace_id on persisted records and surfacing it to
    clients (e.g. SSE error envelopes).
    """
    sp = trace.get_current_span()
    ctx = sp.get_span_context() if sp else None
    if ctx is None or not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x")
