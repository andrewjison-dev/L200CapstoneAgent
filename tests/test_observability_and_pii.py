"""Tests for Category 4: Observability & Tracing (JSON Logs, Intent/Outcome, OTel Spans, PII)."""

from __future__ import annotations

import json
import logging
import unittest

from concierge_agent.observability import (
    PIIRedactionPipeline,
    get_audit_logger,
    get_tracer,
)
from concierge_agent.observability.structured_logger import CloudStructuredJsonFormatter
from concierge_agent.tools import fetch_family_dietary_profile_and_schedule


class TestObservabilityAndPII(unittest.TestCase):
    def test_structured_json_formatter_and_pii_scrubbing(self) -> None:
        redactor = PIIRedactionPipeline()
        dirty = (
            "Contact family@example.com or (312) 555-0199 at 742 Evergreen Terrace; "
            "card 4111-2222-3333-4444; key AIzaSyDummyKey1234567890abcdef."
        )
        clean = redactor.scrub_text(dirty)
        self.assertNotIn("family@example.com", clean)
        self.assertNotIn("555-0199", clean)
        self.assertNotIn("742 Evergreen Terrace", clean)
        self.assertNotIn("4111-2222-3333-4444", clean)
        self.assertNotIn("AIzaSyDummyKey", clean)
        self.assertIn("[REDACTED_EMAIL]", clean)
        self.assertIn("[REDACTED_PHONE]", clean)
        self.assertIn("[REDACTED_HOME_ADDRESS]", clean)
        self.assertIn("[REDACTED_PAYMENT_CARD]", clean)

        formatter = CloudStructuredJsonFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="User email is parent@example.com",
            args=(),
            exc_info=None,
        )
        formatted = json.loads(formatter.format(record))
        self.assertEqual(formatted["severity"], "INFO")
        self.assertIn("[REDACTED_EMAIL]", formatted["message"])

    def test_linked_opentelemetry_spans_and_intent_outcome_pairs(self) -> None:
        tracer = get_tracer()
        audit = get_audit_logger()
        tracer.clear_spans()
        audit.clear_entries()

        with tracer.start_span("root_user_query") as root_span:
            fetch_family_dietary_profile_and_schedule()

        spans = tracer.get_recent_spans(trace_id=root_span.trace_id)
        self.assertGreaterEqual(len(spans), 2)
        child_span = next(
            s for s in spans if s["name"] == "tool.fetch_family_dietary_profile_and_schedule"
        )
        self.assertEqual(child_span["parent_span_id"], root_span.span_id)

        entries = audit.get_recent_entries(limit=10)
        phases = [e["event_phase"] for e in entries]
        self.assertIn("INTENT", phases)
        self.assertIn("OUTCOME", phases)
        intents = {e["correlation_id"] for e in entries if e["event_phase"] == "INTENT"}
        outcomes = {e["correlation_id"] for e in entries if e["event_phase"] == "OUTCOME"}
        self.assertTrue(intents.intersection(outcomes))


if __name__ == "__main__":
    unittest.main()
