"""Structured JSON Logging and Intent-vs-Outcome Audit Capture.

Replaces raw `print()` debugging with structured JSON log records correlated with
OpenTelemetry `trace_id`/`span_id`, scrubbed through `PIIRedactionPipeline`, and
paired into explicit `INTENT` (pre-execution) and `OUTCOME` (post-execution) audit entries.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import sys
from typing import Any
import uuid

from ..config.settings import get_settings
from .pii_redactor import get_pii_redactor
from .tracing import get_tracer

try:
    from pythonjsonlogger import jsonlogger  # type: ignore[import-untyped]

    HAS_JSON_LOGGER = True
except ImportError:  # pragma: no cover
    HAS_JSON_LOGGER = False
    jsonlogger = None


class CloudStructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line structured JSON compatible with Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        settings = get_settings()
        trace_id, span_id = get_tracer().current_trace_context()
        redactor = get_pii_redactor()

        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": redactor.scrub_text(record.getMessage()),
            "service": settings.otel_service_name,
            "environment": settings.env_mode,
        }
        if trace_id:
            payload["logging.googleapis.com/trace"] = (
                f"projects/{settings.gcp_project_id}/traces/{trace_id}"
            )
            payload["trace_id"] = trace_id
        if span_id:
            payload["logging.googleapis.com/spanId"] = span_id
            payload["span_id"] = span_id

        extra_metadata = getattr(record, "structured_data", None)
        if isinstance(extra_metadata, dict):
            payload.update(redactor.scrub_structure(extra_metadata))

        return json.dumps(payload, sort_keys=True)


def get_structured_logger(name: str = "concierge_agent") -> logging.Logger:
    """Returns a configured structured JSON logger with active PII redaction."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(CloudStructuredJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class IntentOutcomeAuditLogger:
    """Explicitly captures the agent's INTENDED action before execution and ACTUAL outcome after."""

    def __init__(self, max_entries: int = 500) -> None:
        self._logger = get_structured_logger("concierge_agent.audit")
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)

    def record_intent(
        self,
        *,
        actor: str,
        action_name: str,
        rationale: str,
        parameters: dict[str, Any],
    ) -> str:
        """Logs an `INTENT` event prior to executing a tool or sub-agent step.

        Returns:
            `correlation_id` linking this intent to its subsequent `OUTCOME` record.
        """
        correlation_id = f"act-{uuid.uuid4().hex[:12]}"
        trace_id, span_id = get_tracer().current_trace_context()
        scrubbed_params = get_pii_redactor().scrub_structure(parameters)
        entry = {
            "event_phase": "INTENT",
            "correlation_id": correlation_id,
            "actor": actor,
            "action_name": action_name,
            "rationale": get_pii_redactor().scrub_text(rationale),
            "intended_parameters": scrubbed_params,
            "trace_id": trace_id,
            "span_id": span_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._entries.append(entry)
        self._logger.info(
            f"INTENT: {actor} -> {action_name}",
            extra={"structured_data": entry},
        )
        return correlation_id

    def record_outcome(
        self,
        *,
        correlation_id: str,
        actor: str,
        action_name: str,
        status: str,
        outcome_summary: str,
        duration_ms: float,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Logs the `OUTCOME` event paired with a preceding `INTENT` via `correlation_id`."""
        trace_id, span_id = get_tracer().current_trace_context()
        entry = {
            "event_phase": "OUTCOME",
            "correlation_id": correlation_id,
            "actor": actor,
            "action_name": action_name,
            "actual_status": status,
            "outcome_summary": get_pii_redactor().scrub_text(outcome_summary),
            "duration_ms": round(duration_ms, 3),
            "outcome_metadata": get_pii_redactor().scrub_structure(metadata or {}),
            "trace_id": trace_id,
            "span_id": span_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._entries.append(entry)
        self._logger.info(
            f"OUTCOME: {actor} -> {action_name} [{status}]",
            extra={"structured_data": entry},
        )
        return entry

    def get_recent_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        """Returns recent structured INTENT and OUTCOME audit records."""
        return list(self._entries)[-limit:]

    def clear_entries(self) -> None:
        """Clears buffered audit records (used in unit tests)."""
        self._entries.clear()


_DEFAULT_AUDIT_LOGGER: IntentOutcomeAuditLogger | None = None


def get_audit_logger() -> IntentOutcomeAuditLogger:
    """Returns the singleton IntentOutcomeAuditLogger."""
    global _DEFAULT_AUDIT_LOGGER
    if _DEFAULT_AUDIT_LOGGER is None:
        _DEFAULT_AUDIT_LOGGER = IntentOutcomeAuditLogger()
    return _DEFAULT_AUDIT_LOGGER
