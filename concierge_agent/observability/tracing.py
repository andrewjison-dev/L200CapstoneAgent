"""Distributed Tracing via OpenTelemetry linking end-to-end request spans.

Configures OpenTelemetry `TracerProvider` (with Google Cloud Trace exporter in cloud mode)
and maintains a bounded ring buffer of parent-child `SpanRecord` objects so both
OpenTelemetry collectors and the Command Center telemetry drawer can inspect full traces.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
import time
from typing import Any, Iterator
import uuid

from ..config.settings import AgentSettings, get_settings
from .pii_redactor import get_pii_redactor

try:
    from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]
    from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
        BatchSpanProcessor,
        InMemorySpanExporter,
    )

    HAS_OTEL = True
except ImportError:  # pragma: no cover
    HAS_OTEL = False
    otel_trace = None

_CURRENT_TRACE_ID: ContextVar[str | None] = ContextVar("concierge_trace_id", default=None)
_CURRENT_SPAN_ID: ContextVar[str | None] = ContextVar("concierge_span_id", default=None)


@dataclass
class SpanRecord:
    """Serializable OpenTelemetry-compatible span linking query-to-answer execution."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_time_ns: int
    end_time_ns: int = 0
    duration_ms: float = 0.0
    status: str = "OK"
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ActiveSpanHandle:
    """Handle yielded inside `tracer.start_span(...)` allowing attribute & status updates."""

    def __init__(self, record: SpanRecord, otel_span: Any | None = None) -> None:
        self.record = record
        self._otel_span = otel_span

    @property
    def trace_id(self) -> str:
        return self.record.trace_id

    @property
    def span_id(self) -> str:
        return self.record.span_id

    def set_attribute(self, key: str, value: Any) -> None:
        scrubbed_val = get_pii_redactor().scrub_structure(value)
        self.record.attributes[key] = scrubbed_val
        if self._otel_span is not None:
            try:
                self._otel_span.set_attribute(key, str(scrubbed_val))
            except Exception:
                pass

    def set_status(self, status: str, description: str | None = None) -> None:
        self.record.status = status
        if description:
            self.record.attributes["status_description"] = (
                get_pii_redactor().scrub_text(description)
            )


class TracingManager:
    """Manages OpenTelemetry TracerProvider and linked parent-child request spans."""

    def __init__(self, settings: AgentSettings | None = None, max_spans: int = 500) -> None:
        self._settings = settings or get_settings()
        self._completed_spans: deque[SpanRecord] = deque(maxlen=max_spans)
        self._otel_tracer: Any | None = None
        self._otel_memory_exporter: Any | None = None
        self._configure_opentelemetry()

    def _configure_opentelemetry(self) -> None:
        if not HAS_OTEL:
            return
        resource = Resource.create(
            {
                "service.name": self._settings.otel_service_name,
                "service.environment": self._settings.env_mode,
            }
        )
        provider = TracerProvider(resource=resource)
        self._otel_memory_exporter = InMemorySpanExporter()
        provider.add_span_processor(BatchSpanProcessor(self._otel_memory_exporter))

        if self._settings.is_cloud_enabled:
            try:
                from opentelemetry.exporter.cloud_trace import (  # type: ignore[import-not-found]
                    CloudTraceSpanExporter,
                )

                provider.add_span_processor(
                    BatchSpanProcessor(
                        CloudTraceSpanExporter(project_id=self._settings.gcp_project_id)
                    )
                )
            except Exception:
                pass

        otel_trace.set_tracer_provider(provider)
        self._otel_tracer = otel_trace.get_tracer(self._settings.otel_service_name)

    def current_trace_context(self) -> tuple[str | None, str | None]:
        """Returns `(trace_id, span_id)` for structured log correlation."""
        return _CURRENT_TRACE_ID.get(), _CURRENT_SPAN_ID.get()

    @contextmanager
    def start_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[ActiveSpanHandle]:
        """Creates a child span linked to the active trace (or starts a new root trace)."""
        parent_trace_id = _CURRENT_TRACE_ID.get()
        parent_span_id = _CURRENT_SPAN_ID.get()
        trace_id = parent_trace_id or uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]

        scrubbed_attrs = (
            get_pii_redactor().scrub_structure(attributes) if attributes else {}
        )
        record = SpanRecord(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            start_time_ns=time.time_ns(),
            attributes=scrubbed_attrs,
        )

        trace_token = _CURRENT_TRACE_ID.set(trace_id)
        span_token = _CURRENT_SPAN_ID.set(span_id)
        start_perf = time.perf_counter()

        if self._otel_tracer is not None:
            with self._otel_tracer.start_as_current_span(name) as otel_span:
                handle = ActiveSpanHandle(record=record, otel_span=otel_span)
                for k, v in scrubbed_attrs.items():
                    handle.set_attribute(k, v)
                try:
                    yield handle
                except Exception as exc:
                    handle.set_status("ERROR", str(exc))
                    raise
                finally:
                    record.end_time_ns = time.time_ns()
                    record.duration_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
                    self._completed_spans.append(record)
                    _CURRENT_SPAN_ID.reset(span_token)
                    _CURRENT_TRACE_ID.reset(trace_token)
        else:
            handle = ActiveSpanHandle(record=record)
            try:
                yield handle
            except Exception as exc:
                handle.set_status("ERROR", str(exc))
                raise
            finally:
                record.end_time_ns = time.time_ns()
                record.duration_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
                self._completed_spans.append(record)
                _CURRENT_SPAN_ID.reset(span_token)
                _CURRENT_TRACE_ID.reset(trace_token)

    def get_recent_spans(self, trace_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Returns recent completed spans, optionally filtered by `trace_id`."""
        spans = list(self._completed_spans)
        if trace_id:
            spans = [s for s in spans if s.trace_id == trace_id]
        return [s.to_dict() for s in spans[-limit:]]

    def clear_spans(self) -> None:
        """Clears recorded in-memory spans (useful in deterministic unit tests)."""
        self._completed_spans.clear()


_DEFAULT_TRACER: TracingManager | None = None


def get_tracer() -> TracingManager:
    """Returns the singleton TracingManager."""
    global _DEFAULT_TRACER
    if _DEFAULT_TRACER is None:
        _DEFAULT_TRACER = TracingManager()
    return _DEFAULT_TRACER
