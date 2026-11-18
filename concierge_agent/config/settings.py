"""Isolated runtime settings with explicit placeholders for external integrations.

All external project IDs, datastore IDs, and secret identifiers default to explicit
`{{PLACEHOLDER_*}}` tokens so the agent operates in complete isolation until deployed.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class AgentSettings:
    """Immutable runtime settings loaded from environment variables."""

    env_mode: str
    gcp_project_id: str
    gcp_location: str
    vertex_search_datastore_id: str
    vertex_memory_bank_agent_engine_id: str
    model_armor_template_id: str
    family_calendar_id: str
    grocery_partner_store_id: str
    gemini_api_key_secret_id: str
    grocery_api_secret_id: str
    calendar_oauth_secret_id: str
    session_database_url: str
    local_state_dir: Path
    fast_model_name: str
    planning_model_name: str
    otel_service_name: str
    default_weekly_budget_usd: float
    hitl_approval_threshold_usd: float
    max_weeknight_cook_minutes: int

    @property
    def is_cloud_enabled(self) -> bool:
        """Returns True only when cloud mode is explicitly enabled and placeholders are replaced."""
        return (
            self.env_mode.lower() == "cloud"
            and not self.gcp_project_id.startswith("{{PLACEHOLDER_")
            and bool(self.gcp_project_id.strip())
        )


def get_settings() -> AgentSettings:
    """Loads agent configuration from environment variables with safe isolated defaults."""
    default_state_dir = Path(
        os.getenv("CONCIERGE_STATE_DIR", ".concierge_state")
    ).resolve()
    default_db_url = f"sqlite:///{default_state_dir / 'concierge_sessions.db'}"

    return AgentSettings(
        env_mode=os.getenv("CONCIERGE_ENV_MODE", "isolated"),
        gcp_project_id=os.getenv(
            "GOOGLE_CLOUD_PROJECT", "{{PLACEHOLDER_GCP_PROJECT_ID}}"
        ),
        gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        vertex_search_datastore_id=os.getenv(
            "VERTEX_SEARCH_DATASTORE_ID",
            "{{PLACEHOLDER_VERTEX_SEARCH_DATASTORE_ID}}",
        ),
        vertex_memory_bank_agent_engine_id=os.getenv(
            "VERTEX_MEMORY_BANK_AGENT_ENGINE_ID",
            "{{PLACEHOLDER_AGENT_ENGINE_ID}}",
        ),
        model_armor_template_id=os.getenv(
            "MODEL_ARMOR_TEMPLATE_ID",
            "{{PLACEHOLDER_MODEL_ARMOR_TEMPLATE_ID}}",
        ),
        family_calendar_id=os.getenv(
            "FAMILY_CALENDAR_ID",
            "{{PLACEHOLDER_FAMILY_CALENDAR_ID}}",
        ),
        grocery_partner_store_id=os.getenv(
            "GROCERY_PARTNER_STORE_ID",
            "{{PLACEHOLDER_GROCERY_STORE_ID}}",
        ),
        gemini_api_key_secret_id=os.getenv(
            "GEMINI_API_KEY_SECRET_ID",
            "{{PLACEHOLDER_GEMINI_API_KEY_SECRET_ID}}",
        ),
        grocery_api_secret_id=os.getenv(
            "GROCERY_PROVIDER_API_SECRET_ID",
            "{{PLACEHOLDER_GROCERY_API_SECRET_ID}}",
        ),
        calendar_oauth_secret_id=os.getenv(
            "FAMILY_CALENDAR_OAUTH_SECRET_ID",
            "{{PLACEHOLDER_CALENDAR_OAUTH_SECRET_ID}}",
        ),
        session_database_url=os.getenv("SESSION_DATABASE_URL", default_db_url),
        local_state_dir=default_state_dir,
        fast_model_name=os.getenv("FAST_MODEL_NAME", "gemini-2.5-flash"),
        planning_model_name=os.getenv("PLANNING_MODEL_NAME", "gemini-2.5-pro"),
        otel_service_name=os.getenv(
            "OTEL_SERVICE_NAME", "andrew-family-concierge-agent"
        ),
        default_weekly_budget_usd=float(
            os.getenv("GROCERY_BUDGET_DEFAULT_USD", "225.00")
        ),
        hitl_approval_threshold_usd=float(
            os.getenv("GROCERY_HITL_APPROVAL_THRESHOLD_USD", "1.00")
        ),
        max_weeknight_cook_minutes=int(
            os.getenv("MAX_WEEKNIGHT_ACTIVE_COOK_MINUTES", "30")
        ),
    )
