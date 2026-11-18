"""Intake & Household Profile Tools surfacing inputs needed from Andrew before planning."""

from __future__ import annotations

import time
from typing import Any

from ..memory.persistent_store import get_memory_store
from ..observability.structured_logger import get_audit_logger
from ..observability.tracing import get_tracer
from ..schemas.models import (
    SchemaValidationError,
    ToolErrorEnvelopeSchema,
    WeeklyBlockerCheckInputSchema,
    WeeklyBlockerCheckOutputSchema,
    validate_payload,
)


def fetch_family_dietary_profile_and_schedule(
    include_pantry_inventory: bool = True,
    memory_query: str = "kid preferences and morning workout routine",
) -> dict[str, Any]:
    """Fetches Andrew's persisted household schedule, allergen rules, pantry stock, and memories.

    Retrieves the canonical daily cadence (05:00-06:00 CST workout, 06:00-06:40 CST family
    breakfast, 08:00-17:00 CST work block, post-17:00 family dinner & core memories),
    excluded household allergens, pantry staples, and top semantic preference memories.

    Args:
        include_pantry_inventory: Whether to include current pantry/fridge staple quantities
            so shopping consolidation can subtract items already on hand.
        memory_query: Natural-language query used to retrieve top semantic meal memories
            from the persistent vector store.

    Returns:
        Dictionary containing `status`, `family_profile`, and `recalled_memories`, or a
        guided `ToolErrorEnvelopeSchema` dictionary if retrieval fails.
    """
    started = time.perf_counter()
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="intake_tools",
        action_name="fetch_family_dietary_profile_and_schedule",
        rationale="Load Andrew's rigid daily schedule, family allergens, and pantry state prior to planning.",
        parameters={
            "include_pantry_inventory": include_pantry_inventory,
            "memory_query": memory_query,
        },
    )

    with get_tracer().start_span(
        "tool.fetch_family_dietary_profile_and_schedule",
        attributes={"include_pantry_inventory": include_pantry_inventory},
    ):
        try:
            store = get_memory_store()
            profile = dict(store.get_state("family_profile", default={}))
            if not include_pantry_inventory:
                profile = {k: v for k, v in profile.items() if k != "pantry_inventory"}

            memories = store.semantic_search_memories(memory_query, top_k=4)
            output = {
                "status": "success",
                "family_profile": profile,
                "recalled_memories": memories,
            }
            audit.record_outcome(
                correlation_id=corr_id,
                actor="intake_tools",
                action_name="fetch_family_dietary_profile_and_schedule",
                status="success",
                outcome_summary=f"Loaded profile and {len(memories)} semantic memories.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return output
        except Exception as exc:
            err = ToolErrorEnvelopeSchema(
                error_code="PROFILE_LOOKUP_FAILED",
                message=f"Could not read household profile: {exc}",
                recovery_instructions=(
                    "Retry `fetch_family_dietary_profile_and_schedule(include_pantry_inventory=False)` "
                    "or ask Andrew directly for any allergen or schedule overrides."
                ),
            ).to_dict()
            audit.record_outcome(
                correlation_id=corr_id,
                actor="intake_tools",
                action_name="fetch_family_dietary_profile_and_schedule",
                status="error",
                outcome_summary=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return err


def collect_weekly_blocker_inputs_for_andrew(
    week_start_date: str,
    late_work_or_travel_days: list[str] | None = None,
    sunday_prep_minutes_available: int | None = None,
    weekly_budget_usd: float | None = None,
    workout_focus: str | None = None,
    use_up_fridge_items: list[str] | None = None,
    kid_or_spouse_cravings: list[str] | None = None,
    use_standard_defaults: bool = False,
) -> dict[str, Any]:
    """Evaluates whether Andrew has provided the weekly check-in inputs needed before orchestrating.

    Enforces the Command Center's Intake-First rule: before generating a 7-day meal plan
    or grocery cart, the agent checks whether Sunday prep time, budget cap, and schedule
    exceptions have been confirmed (or whether Andrew opted into `use_standard_defaults=True`).

    Args:
        week_start_date: ISO-8601 Monday date string (`YYYY-MM-DD`) for the upcoming week.
        late_work_or_travel_days: Optional list of weekdays (e.g., `["Wednesday"]`) when
            Andrew or his spouse need an ultra-fast 15-minute dinner.
        sunday_prep_minutes_available: Minutes available on Sunday for batch meal prep (0-180).
        weekly_budget_usd: Weekly grocery budget ceiling in USD (50.0 to 1000.0).
        workout_focus: Morning 05:00-06:00 CST workout emphasis (e.g., `'strength_hypertrophy'`).
        use_up_fridge_items: Perishable items already in the fridge to use up first.
        kid_or_spouse_cravings: Specific meal requests from Andrew's spouse or 2 kids.
        use_standard_defaults: When True, fills any omitted fields from Andrew's saved defaults
            and marks `ready_to_orchestrate=True` immediately.

    Returns:
        Validated `WeeklyBlockerCheckOutputSchema` dictionary containing `ready_to_orchestrate`,
        `missing_inputs`, a concise `prompt_for_andrew`, and `normalized_weekly_context`.
    """
    started = time.perf_counter()
    raw_input = {
        "week_start_date": week_start_date,
        "late_work_or_travel_days": late_work_or_travel_days or [],
        "sunday_prep_minutes_available": sunday_prep_minutes_available,
        "weekly_budget_usd": weekly_budget_usd,
        "workout_focus": workout_focus,
        "use_up_fridge_items": use_up_fridge_items or [],
        "kid_or_spouse_cravings": kid_or_spouse_cravings or [],
        "use_standard_defaults": use_standard_defaults,
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="intake_tools",
        action_name="collect_weekly_blocker_inputs_for_andrew",
        rationale="Verify pre-planning check-in inputs from Andrew before orchestrating the week.",
        parameters=raw_input,
    )

    with get_tracer().start_span(
        "tool.collect_weekly_blocker_inputs_for_andrew",
        attributes={"week_start_date": week_start_date, "use_standard_defaults": use_standard_defaults},
    ):
        try:
            validated = validate_payload(WeeklyBlockerCheckInputSchema, raw_input)
        except SchemaValidationError as exc:
            err = ToolErrorEnvelopeSchema(
                error_code="INVALID_WEEKLY_CHECKIN_INPUT",
                message=str(exc),
                recovery_instructions=(
                    "Pass `week_start_date` in strict 'YYYY-MM-DD' format (e.g. '2026-11-23'), "
                    "`sunday_prep_minutes_available` between 0 and 180, and `weekly_budget_usd` "
                    "between 50.0 and 1000.0."
                ),
                invalid_fields=["week_start_date", "sunday_prep_minutes_available", "weekly_budget_usd"],
            ).to_dict()
            audit.record_outcome(
                correlation_id=corr_id,
                actor="intake_tools",
                action_name="collect_weekly_blocker_inputs_for_andrew",
                status="error",
                outcome_summary=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return err

        missing: list[str] = []
        if not validated.use_standard_defaults:
            if validated.sunday_prep_minutes_available is None:
                missing.append("sunday_prep_minutes_available")
            if validated.weekly_budget_usd is None:
                missing.append("weekly_budget_usd")
            if validated.workout_focus is None:
                missing.append("workout_focus")

        normalized = {
            "week_start_date": validated.week_start_date,
            "late_work_or_travel_days": validated.late_work_or_travel_days,
            "sunday_prep_minutes_available": (
                validated.sunday_prep_minutes_available
                if validated.sunday_prep_minutes_available is not None
                else 75
            ),
            "weekly_budget_usd": (
                validated.weekly_budget_usd
                if validated.weekly_budget_usd is not None
                else 225.0
            ),
            "workout_focus": validated.workout_focus or "balanced_strength_and_wellness",
            "use_up_fridge_items": validated.use_up_fridge_items,
            "kid_or_spouse_cravings": validated.kid_or_spouse_cravings,
        }

        ready = len(missing) == 0
        if ready:
            get_memory_store().set_state("active_weekly_context", normalized)
            prompt_msg = (
                "All weekly check-in inputs are confirmed! Ready to orchestrate Andrew's "
                "7-day meal plan, Sunday batch-prep gameplan, and aisle-sorted grocery cart."
            )
        else:
            prompt_msg = (
                "Quick 60-Second Sunday Check-In for Andrew before I orchestrate your week:\n"
                "1. Sunday Batch-Prep Window: Do you have ~75 minutes this Sunday (or prefer 0-min no-prep)?\n"
                "2. Schedule Exceptions: Any post-5PM late meetings, kid events, or travel nights?\n"
                "3. 5AM Workout Focus & Cravings: Strength/high-protein focus, fridge items to use up, or kid requests?\n"
                "4. Weekly Grocery Budget: Keep your standard $225 cap? (Or reply 'Use my defaults' to run immediately!)"
            )

        result = WeeklyBlockerCheckOutputSchema(
            ready_to_orchestrate=ready,
            missing_inputs=missing,
            prompt_for_andrew=prompt_msg,
            normalized_weekly_context=normalized,
        ).to_dict()

        audit.record_outcome(
            correlation_id=corr_id,
            actor="intake_tools",
            action_name="collect_weekly_blocker_inputs_for_andrew",
            status="success",
            outcome_summary=f"ready_to_orchestrate={ready}, missing={missing}",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return result
