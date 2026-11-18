"""Rapid Cook-Mode Timelines, HITL Calendar Hold Publishing, and Async Family Feedback Tools."""

from __future__ import annotations

import time
from typing import Any

from ..config.secrets import get_secret_provider
from ..config.settings import get_settings
from ..memory.async_memory_worker import get_async_memory_worker
from ..observability.structured_logger import get_audit_logger
from ..observability.tracing import get_tracer
from ..orchestration.hitl_hooks import get_approval_gate
from ..schemas.models import (
    CookingStepSchema,
    CookingTimelineOutputSchema,
    CookingTimelineRequestSchema,
    MealFeedbackInputSchema,
    SchemaValidationError,
    ToolErrorEnvelopeSchema,
    validate_payload,
)
from .catalog_data import CURATED_RECIPES


def generate_step_by_step_cooking_timeline(
    recipe_id: str,
    target_serve_time_cst: str = "17:45 CST",
    sunday_prep_completed: bool = True,
) -> dict[str, Any]:
    """Generates a calm, parallelized Cook-Mode timeline with kid-friendly split checkpoints.

    Args:
        recipe_id: Identifier of the recipe in the curated catalog (`dinner_...` or `bfast_...`).
        target_serve_time_cst: Target table time in `America/Chicago` (e.g. `'17:45 CST'`).
        sunday_prep_completed: Whether Sunday batch-prep was completed (reduces active minutes).

    Returns:
        Validated `CookingTimelineOutputSchema` dictionary with timed steps and kid checkpoints,
        or a guided error envelope if `recipe_id` is unknown.
    """
    started = time.perf_counter()
    raw_args = {
        "recipe_id": recipe_id,
        "target_serve_time_cst": target_serve_time_cst,
        "sunday_prep_completed": sunday_prep_completed,
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="cooking_and_feedback_tools",
        action_name="generate_step_by_step_cooking_timeline",
        rationale=f"Build step-by-step Cook Mode timeline for '{recipe_id}'.",
        parameters=raw_args,
    )

    with get_tracer().start_span(
        "tool.generate_step_by_step_cooking_timeline",
        attributes={"recipe_id": recipe_id, "sunday_prep_completed": sunday_prep_completed},
    ):
        try:
            validated = validate_payload(CookingTimelineRequestSchema, raw_args)
        except SchemaValidationError as exc:
            return ToolErrorEnvelopeSchema(
                error_code="INVALID_COOKING_TIMELINE_REQUEST",
                message=str(exc),
                recovery_instructions="Provide a valid `recipe_id` string (at least 3 chars).",
                invalid_fields=["recipe_id"],
            ).to_dict()

        recipe = CURATED_RECIPES.get(validated.recipe_id)
        if recipe is None:
            return ToolErrorEnvelopeSchema(
                error_code="RECIPE_NOT_FOUND_FOR_TIMELINE",
                message=f"Unknown recipe_id '{validated.recipe_id}'.",
                recovery_instructions=(
                    "Call `search_high_protein_kid_friendly_recipes` or inspect `daily_schedule` "
                    "in the active weekly meal plan to get a valid `recipe_id`."
                ),
                invalid_fields=["recipe_id"],
            ).to_dict()

        extra_unprepped_mins = 0 if validated.sunday_prep_completed else 6
        steps: list[CookingStepSchema] = []
        if not validated.sunday_prep_completed and recipe.get("sunday_prep_task"):
            steps.append(
                CookingStepSchema(
                    minute_offset=0,
                    station="Prep Board",
                    instruction=f"Quick inline prep: {recipe['sunday_prep_task']}",
                    kid_friendly_checkpoint=None,
                )
            )

        for raw_step in recipe["cook_steps"]:
            steps.append(
                CookingStepSchema(
                    minute_offset=int(raw_step["minute_offset"]) + extra_unprepped_mins,
                    station=raw_step["station"],
                    instruction=raw_step["instruction"],
                    kid_friendly_checkpoint=raw_step.get("kid_friendly_checkpoint"),
                )
            )

        total_mins = int(recipe["active_cook_minutes"]) + extra_unprepped_mins
        out = CookingTimelineOutputSchema(
            recipe_id=validated.recipe_id,
            recipe_title=recipe["title"],
            target_serve_time_cst=validated.target_serve_time_cst,
            total_active_minutes=total_mins,
            steps=steps,
        ).to_dict()

        audit.record_outcome(
            correlation_id=corr_id,
            actor="cooking_and_feedback_tools",
            action_name="generate_step_by_step_cooking_timeline",
            status="success",
            outcome_summary=f"Generated {len(steps)} steps ({total_mins}m active).",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return out


def publish_prep_blocks_to_family_calendar_with_approval(
    plan_id: str,
    sunday_prep_slot_cst: str = "Sunday 15:30-16:45 CST",
    human_approval_confirmed: bool = False,
    approval_token: str | None = None,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Publishes Sunday batch-prep and weeknight dinner holds to the placeholder Family Calendar.

    Guarded by `HumanApprovalGate`: pauses and returns `PENDING_HUMAN_APPROVAL` unless
    Andrew has explicitly confirmed calendar modification.

    Args:
        plan_id: Identifier of the weekly meal plan (`plan-...`).
        sunday_prep_slot_cst: Proposed Sunday batch-prep time block in CST.
        human_approval_confirmed: True only after Andrew explicitly approves calendar writes.
        approval_token: One-time HITL confirmation token.
        tool_context: Optional ADK `ToolContext` for native runner confirmation pauses.

    Returns:
        Dictionary indicating `PENDING_HUMAN_APPROVAL` or isolated calendar hold creation.
    """
    started = time.perf_counter()
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="cooking_and_feedback_tools",
        action_name="publish_prep_blocks_to_family_calendar_with_approval",
        rationale="Schedule Sunday batch-prep hold on family calendar (HITL guarded).",
        parameters={"plan_id": plan_id, "sunday_prep_slot_cst": sunday_prep_slot_cst},
    )

    authorized, pending_gate = get_approval_gate().verify_execution_authorized(
        action_type="family_calendar_publish",
        target_id=plan_id,
        human_approval_confirmed=human_approval_confirmed,
        approval_token=approval_token,
        tool_context=tool_context,
    )
    if not authorized and pending_gate is not None:
        res = {
            "status": "PENDING_HUMAN_APPROVAL",
            "plan_id": plan_id,
            "requires_human_confirmation": True,
            "approval_token": pending_gate["approval_token"],
            "confirmation_prompt": (
                f"Approve adding '{sunday_prep_slot_cst}' batch-prep block and 17:15 CST "
                f"dinner holds for {plan_id} to calendar placeholder "
                f"'{get_settings().family_calendar_id}'?"
            ),
        }
        audit.record_outcome(
            correlation_id=corr_id,
            actor="cooking_and_feedback_tools",
            action_name="publish_prep_blocks_to_family_calendar_with_approval",
            status="PENDING_HUMAN_APPROVAL",
            outcome_summary="Paused at HITL gate before calendar write.",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return res

    settings = get_settings()
    _oauth_token = get_secret_provider().get_secret(settings.calendar_oauth_secret_id)
    del _oauth_token

    out = {
        "status": "CALENDAR_HOLDS_PUBLISHED_ISOLATED",
        "plan_id": plan_id,
        "calendar_target": settings.family_calendar_id,
        "sunday_prep_slot_cst": sunday_prep_slot_cst,
        "requires_human_confirmation": False,
    }
    audit.record_outcome(
        correlation_id=corr_id,
        actor="cooking_and_feedback_tools",
        action_name="publish_prep_blocks_to_family_calendar_with_approval",
        status="CALENDAR_HOLDS_PUBLISHED_ISOLATED",
        outcome_summary="Published isolated family calendar holds after human approval.",
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
    return out


def persist_weekly_family_meal_feedback(
    recipe_id: str,
    andrew_rating: int,
    spouse_rating: int,
    kids_ate_cleanly: bool,
    notes: str = "",
) -> dict[str, Any]:
    """Queues non-blocking async vector memory consolidation for family meal feedback.

    Returns immediately (`status='QUEUED_ASYNC'`) while `AsyncMemoryConsolidationWorker`
    scrubs PII, computes semantic embeddings, and updates persistent SQLite/Vertex memory
    in the background without blocking the Command Center UI.

    Args:
        recipe_id: Catalog recipe identifier rated by the family.
        andrew_rating: Andrew's 1-5 rating (taste + post-workout energy/macros).
        spouse_rating: Spouse's 1-5 rating.
        kids_ate_cleanly: True if both young kids ate their portion happily.
        notes: Optional free-text notes (scrubbed for PII prior to background storage).

    Returns:
        Dictionary containing `status='QUEUED_ASYNC'`, `memory_job_id`, and recipe summary.
    """
    started = time.perf_counter()
    raw_args = {
        "recipe_id": recipe_id,
        "andrew_rating": andrew_rating,
        "spouse_rating": spouse_rating,
        "kids_ate_cleanly": kids_ate_cleanly,
        "notes": notes,
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="cooking_and_feedback_tools",
        action_name="persist_weekly_family_meal_feedback",
        rationale="Queue async background preference consolidation without blocking UI.",
        parameters=raw_args,
    )

    try:
        validated = validate_payload(MealFeedbackInputSchema, raw_args)
    except SchemaValidationError as exc:
        return ToolErrorEnvelopeSchema(
            error_code="INVALID_FEEDBACK_PAYLOAD",
            message=str(exc),
            recovery_instructions=(
                "Pass `andrew_rating` and `spouse_rating` as integers between 1 and 5, "
                "and `kids_ate_cleanly` as a boolean."
            ),
            invalid_fields=["andrew_rating", "spouse_rating"],
        ).to_dict()

    recipe = CURATED_RECIPES.get(validated.recipe_id)
    if recipe is None:
        return ToolErrorEnvelopeSchema(
            error_code="UNKNOWN_RECIPE_FOR_FEEDBACK",
            message=f"Recipe '{validated.recipe_id}' not found.",
            recovery_instructions="Use a valid `recipe_id` from the active meal plan.",
            invalid_fields=["recipe_id"],
        ).to_dict()

    summary_sentence = (
        f"Family feedback on {recipe['title']} ({validated.recipe_id}): "
        f"Andrew={validated.andrew_rating}/5, Spouse={validated.spouse_rating}/5, "
        f"kids_ate_cleanly={validated.kids_ate_cleanly}. Notes: {validated.notes}"
    )
    job_id = get_async_memory_worker().submit_background_consolidation(
        category="family_meal_feedback",
        summary_text=summary_sentence,
        metadata={
            "recipe_id": validated.recipe_id,
            "andrew_rating": validated.andrew_rating,
            "spouse_rating": validated.spouse_rating,
            "kids_ate_cleanly": validated.kids_ate_cleanly,
        },
    )

    res = {
        "status": "QUEUED_ASYNC",
        "memory_job_id": job_id,
        "recipe_id": validated.recipe_id,
        "recipe_title": recipe["title"],
    }
    audit.record_outcome(
        correlation_id=corr_id,
        actor="cooking_and_feedback_tools",
        action_name="persist_weekly_family_meal_feedback",
        status="QUEUED_ASYNC",
        outcome_summary=f"Dispatched non-blocking memory consolidation job {job_id}.",
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
    return res
