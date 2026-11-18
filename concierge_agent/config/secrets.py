"""Secure Secret Management via Google Cloud Secret Manager.

Eliminates hardcoded API keys across all tools and model clients. Secrets are fetched
just-in-time from Google Cloud Secret Manager (`SecretManagerServiceClient`) with
bounded in-memory TTL caching and isolated-mode runtime env injection for local testing.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any

from .settings import AgentSettings, get_settings

try:
    from google.cloud import secretmanager  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - optional in bare offline stdlib runner
    secretmanager = None


class SecretResolutionError(RuntimeError):
    """Raised when a required secret cannot be retrieved securely."""


@dataclass
class _CachedSecret:
    value: str
    expires_at_monotonic: float


class SecretManagerProvider:
    """Retrieves secrets securely from Google Cloud Secret Manager without hardcoding credentials."""

    def __init__(
        self,
        settings: AgentSettings | None = None,
        ttl_seconds: int = 300,
        client: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._ttl_seconds = ttl_seconds
        self._client = client
        self._cache: dict[str, _CachedSecret] = {}

    def _get_gcp_client(self) -> Any:
        if self._client is not None:
            return self._client
        if secretmanager is None:
            raise SecretResolutionError(
                "google-cloud-secret-manager package is not installed. "
                "Install dependencies from requirements.txt or run in CONCIERGE_ENV_MODE=isolated."
            )
        self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def get_secret(self, secret_id: str, version: str = "latest") -> str:
        """Fetches a secret payload from Secret Manager or runtime environment injection.

        Args:
            secret_id: Logical Secret Manager secret identifier or placeholder name.
            version: Secret version to access (defaults to 'latest').

        Returns:
            Resolved secret string (never logged or persisted to disk).

        Raises:
            SecretResolutionError: If the secret cannot be resolved in cloud mode.
        """
        cache_key = f"{secret_id}:{version}"
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at_monotonic > now:
            return cached.value

        # 1. Check explicit runtime-injected environment variable (CI / Cloud Run secret mount)
        sanitized_env_key = (
            secret_id.replace("{{", "")
            .replace("}}", "")
            .replace("-", "_")
            .upper()
        )
        env_override = os.getenv(sanitized_env_key)
        if env_override:
            self._cache[cache_key] = _CachedSecret(
                value=env_override,
                expires_at_monotonic=now + self._ttl_seconds,
            )
            return env_override

        # 2. In isolated mode or when placeholder IDs are active, return a non-credential sentinel
        if not self._settings.is_cloud_enabled or secret_id.startswith("{{PLACEHOLDER_"):
            isolated_token = f"ISOLATED_RUNTIME_PLACEHOLDER::{sanitized_env_key}"
            self._cache[cache_key] = _CachedSecret(
                value=isolated_token,
                expires_at_monotonic=now + self._ttl_seconds,
            )
            return isolated_token

        # 3. Fetch from Google Cloud Secret Manager (`projects/*/secrets/*/versions/*`)
        client = self._get_gcp_client()
        resource_name = (
            f"projects/{self._settings.gcp_project_id}/secrets/{secret_id}/versions/{version}"
        )
        try:
            response = client.access_secret_version(request={"name": resource_name})
            payload = response.payload.data.decode("utf-8")
        except Exception as exc:
            raise SecretResolutionError(
                f"Failed to retrieve secret '{secret_id}' from Secret Manager "
                f"in project '{self._settings.gcp_project_id}'. Verify IAM role "
                "'roles/secretmanager.secretAccessor' on the agent service account."
            ) from exc

        self._cache[cache_key] = _CachedSecret(
            value=payload,
            expires_at_monotonic=now + self._ttl_seconds,
        )
        return payload

    def clear_cache(self) -> None:
        """Clears cached secrets from memory."""
        self._cache.clear()


_DEFAULT_PROVIDER: SecretManagerProvider | None = None


def get_secret_provider() -> SecretManagerProvider:
    """Returns the singleton SecretManagerProvider."""
    global _DEFAULT_PROVIDER
    if _DEFAULT_PROVIDER is None:
        _DEFAULT_PROVIDER = SecretManagerProvider()
    return _DEFAULT_PROVIDER
