"""Tests for ``telos.observability`` and its agent-tool span integration."""

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from telos import observability
from telos.agent import _execute_tool
from telos.domain import DelegationGraph
from telos.domain.tools import ToolContext, ToolResult


def _log_record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="telos.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_init_tracing_is_noop_when_endpoint_unset(monkeypatch):
    """Without an endpoint, init returns False and doesn't block future init."""
    monkeypatch.setattr(observability, "_tracing_provider_is_configured", lambda: False)
    assert observability.init_tracing() is False


def test_current_trace_id_is_none_outside_span():
    """No active span (and no real provider wired) → no trace_id."""
    assert observability.current_trace_id() is None


def test_span_does_not_crash_with_noop_provider():
    """The default no-op tracer provider still services ``span(...)``,
    just doesn't export anything. This is the path most local-dev / test
    code follows, so it must stay panic-free."""
    with observability.span("test.smoke", flag=True, count=1):
        pass


def test_tracer_returns_callable_without_init():
    """``tracer()`` is safe to call before ``init_tracing`` — the OTel
    proxy provider returns a usable (no-op) tracer."""
    t = observability.tracer()
    assert hasattr(t, "start_as_current_span")


def test_init_tracing_empty_call_does_not_prevent_later_enable(monkeypatch):
    """A no-op init must not permanently suppress a later configured init."""
    calls: list[str] = []
    state = {"configured": False}

    class FakeProvider:
        def __init__(self, resource):
            self.resource = resource

        def add_span_processor(self, processor):
            calls.append(f"span-processor:{processor!r}")

    class FakeLoggerProvider:
        def __init__(self, resource):
            self.resource = resource

        def add_log_record_processor(self, processor):
            calls.append(f"log-processor:{processor!r}")

    class FakeExporter:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def __repr__(self):
            return f"exporter:{self.endpoint}"

    class FakeInstrumentor:
        def instrument(self, *args, **kwargs):
            calls.append(f"instrument:{kwargs!r}")

    monkeypatch.setattr(
        observability,
        "_tracing_provider_is_configured",
        lambda: state["configured"],
    )
    monkeypatch.setattr(observability.Resource, "create", lambda attrs: attrs)
    monkeypatch.setattr(observability, "TracerProvider", FakeProvider)
    monkeypatch.setattr(observability, "LoggerProvider", FakeLoggerProvider)
    monkeypatch.setattr(observability, "OTLPSpanExporter", FakeExporter)
    monkeypatch.setattr(observability, "OTLPLogExporter", FakeExporter)
    monkeypatch.setattr(observability, "BatchSpanProcessor", lambda exporter: exporter)
    monkeypatch.setattr(observability, "BatchLogRecordProcessor", lambda exporter: exporter)
    def set_provider(provider):
        state["configured"] = True
        calls.append("provider")

    monkeypatch.setattr(observability.trace, "set_tracer_provider", set_provider)
    monkeypatch.setattr(observability._logs, "set_logger_provider", lambda provider: calls.append("logger-provider"))
    monkeypatch.setattr(
        observability,
        "_add_otel_log_handler_once",
        lambda logger_provider: calls.append("root-handler"),
    )
    monkeypatch.setattr(observability, "HTTPXClientInstrumentor", FakeInstrumentor)
    monkeypatch.setattr(observability, "LoggingInstrumentor", FakeInstrumentor)

    assert observability.init_tracing("") is False
    assert observability.init_tracing("http://collector:4317", "telos-test") is True
    assert "provider" in calls
    assert "logger-provider" in calls
    assert "root-handler" in calls
    assert "span-processor:exporter:http://collector:4317" in calls
    assert "log-processor:exporter:http://collector:4317" in calls
    assert "instrument:{}" in calls
    assert "instrument:{'set_logging_format': False}" in calls
    assert observability.init_tracing("") is True


def test_otel_log_formatter_includes_trace_and_span_ids_when_present():
    fmt = observability.OTelLogFormatter("%(levelname)s %(name)s%(otel_context)s - %(message)s")
    record = _log_record()
    record.otelTraceID = "1234567890abcdef1234567890abcdef"
    record.otelSpanID = "1234567890abcdef"

    rendered = fmt.format(record)

    assert rendered == (
        "INFO telos.test "
        "trace_id=1234567890abcdef1234567890abcdef "
        "span_id=1234567890abcdef - hello"
    )


def test_otel_log_formatter_omits_trace_context_when_fields_absent():
    fmt = observability.OTelLogFormatter("%(levelname)s %(name)s%(otel_context)s - %(message)s")

    rendered = fmt.format(_log_record())

    assert rendered == "INFO telos.test - hello"


def test_otel_log_formatter_omits_zero_otel_ids():
    fmt = observability.OTelLogFormatter("%(levelname)s %(name)s%(otel_context)s - %(message)s")
    record = _log_record()
    record.otelTraceID = "0"
    record.otelSpanID = "0"

    rendered = fmt.format(record)

    assert rendered == "INFO telos.test - hello"


def test_otel_log_formatter_uses_configured_local_timezone(monkeypatch):
    from telos import local_time

    monkeypatch.setenv("TELOS_LOCAL_TIMEZONE", "Europe/London")
    local_time.local_timezone.cache_clear()
    fmt = observability.OTelLogFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M")
    record = _log_record()
    record.created = datetime(2026, 6, 2, 23, 30, tzinfo=UTC).timestamp()

    try:
        assert fmt.format(record) == "2026-06-03 00:30 hello"
    finally:
        local_time.local_timezone.cache_clear()


class _SpanProbe:
    def __init__(self) -> None:
        self.attrs = {}
        self.statuses = []
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attrs[key] = value

    def set_status(self, status):
        self.statuses.append(status)

    def record_exception(self, exc):
        self.exceptions.append(exc)


@contextmanager
def _probe_span(probe: _SpanProbe):
    yield probe


class _Input(BaseModel):
    value: str = ""


class _ErrorTool:
    name = "error_tool"
    description = "Returns an error ToolResult."
    input_model = _Input

    async def __call__(self, inp: _Input, ctx: ToolContext) -> ToolResult:
        return ToolResult(text="tool failed", is_error=True)

    def oai_schema(self):
        return {}


class _ThrowTool:
    name = "throw_tool"
    description = "Raises."
    input_model = _Input

    async def __call__(self, inp: _Input, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("boom")

    def oai_schema(self):
        return {}


@pytest.mark.asyncio
async def test_tool_span_marks_unknown_tool_as_error(monkeypatch, tmp_path: Path):
    from telos import agent as agent_mod

    probe = _SpanProbe()
    monkeypatch.setattr(agent_mod, "span", lambda *args, **kwargs: _probe_span(probe))

    text, is_error, _ = await _execute_tool(
        "missing", {}, {},
        user_id="u", agent_slug="a",
        workspace=tmp_path, storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )

    assert text == "Unknown tool: missing"
    assert is_error is True
    assert probe.attrs["tool.is_error"] is True
    assert probe.statuses


@pytest.mark.asyncio
async def test_tool_span_marks_error_tool_result_as_error(monkeypatch, tmp_path: Path):
    from telos import agent as agent_mod

    probe = _SpanProbe()
    monkeypatch.setattr(agent_mod, "span", lambda *args, **kwargs: _probe_span(probe))

    text, is_error, _ = await _execute_tool(
        "error_tool", {"value": "x"}, {"error_tool": _ErrorTool()},
        user_id="u", agent_slug="a",
        workspace=tmp_path, storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )

    assert text == "tool failed"
    assert is_error is True
    assert probe.attrs["tool.is_error"] is True
    assert probe.statuses


@pytest.mark.asyncio
async def test_tool_span_records_caught_exception(monkeypatch, tmp_path: Path):
    from telos import agent as agent_mod

    probe = _SpanProbe()
    monkeypatch.setattr(agent_mod, "span", lambda *args, **kwargs: _probe_span(probe))

    text, is_error, _ = await _execute_tool(
        "throw_tool", {}, {"throw_tool": _ThrowTool()},
        user_id="u", agent_slug="a",
        workspace=tmp_path, storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )

    assert "Tool throw_tool failed" in text
    assert is_error is True
    assert probe.attrs["tool.is_error"] is True
    assert probe.statuses
    assert len(probe.exceptions) == 1
