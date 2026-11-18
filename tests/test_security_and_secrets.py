"""Tests for Category 5: Secret Management & Zero-Internal-Reference Isolation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import unittest

from concierge_agent.config import SecretManagerProvider, get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent

_FORBIDDEN_CREDENTIAL_REGEX = re.compile(
    r"(?:AIza[0-9A-Za-z\-_]{30,}|sk-[0-9A-Za-z]{30,}|-----BEGIN (?:RSA )?PRIVATE KEY-----)"
)
_FORBIDDEN_INTERNAL_DOC_REGEX = re.compile(
    r"(?:docs\.google\.com/document/d/|corp\.google\.com|googleplex\.com|(?<![A-Za-z])go/[a-z0-9\-]+)"
)


class TestSecurityAndIsolation(unittest.TestCase):
    def test_secret_manager_provider_never_uses_hardcoded_keys(self) -> None:
        settings = get_settings()
        self.assertFalse(settings.is_cloud_enabled)
        provider = SecretManagerProvider(settings=settings)
        token = provider.get_secret(settings.grocery_api_secret_id)
        self.assertTrue(token.startswith("ISOLATED_RUNTIME_PLACEHOLDER::"))

        os.environ["CUSTOM_TEST_SECRET"] = "injected-via-runtime-env"
        try:
            self.assertEqual(
                provider.get_secret("{{CUSTOM_TEST_SECRET}}"),
                "injected-via-runtime-env",
            )
        finally:
            os.environ.pop("CUSTOM_TEST_SECRET", None)

    def test_repository_contains_zero_hardcoded_secrets_and_zero_internal_google_links(self) -> None:
        scanned_files = 0
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if (
                rel.startswith(".git/")
                or rel.startswith(".concierge_state/")
                or "__pycache__" in rel
                or rel == "tests/test_observability_and_pii.py"
                or rel == "tests/test_security_and_secrets.py"
            ):
                continue
            if path.suffix in {".py", ".md", ".json", ".yml", ".yaml", ".tf", ".html", ".js", ".css"}:
                content = path.read_text(encoding="utf-8")
                scanned_files += 1
                self.assertIsNone(
                    _FORBIDDEN_CREDENTIAL_REGEX.search(content),
                    f"Hardcoded credential pattern detected in {rel}",
                )
                self.assertIsNone(
                    _FORBIDDEN_INTERNAL_DOC_REGEX.search(content),
                    f"Forbidden internal document/URL reference detected in {rel}",
                )
        self.assertGreaterEqual(scanned_files, 20)


if __name__ == "__main__":
    unittest.main()
