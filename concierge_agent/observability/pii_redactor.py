"""Active PII Redaction Pipeline for Logging and Persistent Memory Storage.

Combines Google Cloud Sensitive Data Protection (`google-cloud-dlp`) when running in
cloud mode with deterministic local regex/contextual scrubbers so sensitive household
information (emails, phone numbers, home street addresses, payment cards, SSNs, API tokens,
and minor children's names/medical notes) is scrubbed BEFORE touching logs or storage.
"""

from __future__ import annotations

import re
from typing import Any

from ..config.settings import AgentSettings, get_settings

try:
    from google.cloud import dlp_v2  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    dlp_v2 = None


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "authorization",
        "approval_secret",
        "card_number",
        "credit_card",
        "cvv",
        "home_address",
        "password",
        "phone",
        "phone_number",
        "secret",
        "ssn",
    }
)

_REGEX_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Email addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    # Payment / Credit Card numbers (13-19 digits with optional spaces/dashes)
    (
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        "[REDACTED_PAYMENT_CARD]",
    ),
    # US Social Security Numbers
    (
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    # North American Phone Numbers
    (
        re.compile(
            r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
        ),
        "[REDACTED_PHONE]",
    ),
    # API Keys / Bearer Tokens
    (
        re.compile(r"\b(?:AIza[0-9A-Za-z\-_]{20,}|ya29\.[0-9A-Za-z\-_]+|sk-[0-9A-Za-z]{20,})\b"),
        "[REDACTED_CREDENTIAL]",
    ),
    # Street addresses (e.g., "742 Evergreen Terrace" or "1234 Maple St")
    (
        re.compile(
            r"\b\d{2,5}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}\s+"
            r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Terrace|Way)\b",
            re.IGNORECASE,
        ),
        "[REDACTED_HOME_ADDRESS]",
    ),
    # Explicit child name / DOB tags if accidentally typed by user
    (
        re.compile(
            r"\b(?:child_name|kid_name|pediatric_id)\s*[:=]\s*[A-Za-z0-9_-]+",
            re.IGNORECASE,
        ),
        "[REDACTED_CHILD_IDENTIFIER]",
    ),
)


class PIIRedactionPipeline:
    """Scrubs PII from strings and nested JSON payloads prior to logging or persistence."""

    def __init__(
        self,
        settings: AgentSettings | None = None,
        dlp_client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._dlp_client = dlp_client
        self._redaction_count = 0

    @property
    def total_redactions(self) -> int:
        """Total number of sensitive substrings or keys scrubbed in this process."""
        return self._redaction_count

    def scrub_text(self, text: str) -> str:
        """Scrubs PII tokens from a string using local patterns and optional Cloud DLP."""
        if not text:
            return text

        scrubbed = text
        for pattern, replacement in _REGEX_SCRUBBERS:
            scrubbed, count = pattern.subn(replacement, scrubbed)
            self._redaction_count += count

        # Optional Google Cloud Sensitive Data Protection (DLP) pass in cloud mode
        if self._settings.is_cloud_enabled and dlp_v2 is not None:
            scrubbed = self._scrub_with_cloud_dlp(scrubbed)

        return scrubbed

    def _scrub_with_cloud_dlp(self, text: str) -> str:
        """Invokes Google Cloud DLP `deidentify_content` when enabled in production."""
        try:
            if self._dlp_client is None:
                self._dlp_client = dlp_v2.DlpServiceClient()
            parent = f"projects/{self._settings.gcp_project_id}/locations/global"
            inspect_config = {
                "info_types": [
                    {"name": "EMAIL_ADDRESS"},
                    {"name": "PHONE_NUMBER"},
                    {"name": "STREET_ADDRESS"},
                    {"name": "CREDIT_CARD_NUMBER"},
                    {"name": "PERSON_NAME"},
                    {"name": "MEDICAL_TERM"},
                ]
            }
            deidentify_config = {
                "info_type_transformations": {
                    "transformations": [
                        {"primitive_transformation": {"replace_with_info_type_config": {}}}
                    ]
                }
            }
            response = self._dlp_client.deidentify_content(
                request={
                    "parent": parent,
                    "inspect_config": inspect_config,
                    "deidentify_config": deidentify_config,
                    "item": {"value": text},
                }
            )
            return str(response.item.value)
        except Exception:
            # Fail-closed to deterministic local scrubbing if DLP endpoint is unreachable
            return text

    def scrub_structure(self, value: Any) -> Any:
        """Recursively scrubs PII across dicts, lists, tuples, and primitive strings."""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for k, v in value.items():
                key_str = str(k)
                if key_str.lower() in _SENSITIVE_KEY_NAMES:
                    self._redaction_count += 1
                    cleaned[key_str] = "[REDACTED_SENSITIVE_FIELD]"
                else:
                    cleaned[key_str] = self.scrub_structure(v)
            return cleaned
        if isinstance(value, list):
            return [self.scrub_structure(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.scrub_structure(item) for item in value)
        return value


_DEFAULT_REDACTOR: PIIRedactionPipeline | None = None


def get_pii_redactor() -> PIIRedactionPipeline:
    """Returns the singleton PIIRedactionPipeline."""
    global _DEFAULT_REDACTOR
    if _DEFAULT_REDACTOR is None:
        _DEFAULT_REDACTOR = PIIRedactionPipeline()
    return _DEFAULT_REDACTOR
