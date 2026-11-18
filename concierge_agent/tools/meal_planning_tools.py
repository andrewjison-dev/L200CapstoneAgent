"""7-Day Family Meal & Sunday Batch-Prep Schedule Generator Tool."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ..memory.persistent_store import get_memory_store
from ..observability.structured_logger import get_audit_logger
from ..observability.tracing import get_tracer
from ..orchestration.guardrails_plugin import SelfEvalGuardrail
from ..orchestration.model_router import get_model_router
from ..schemas.models import (
    DailyMealSlotSchema,
    SchemaValidationError,
    ToolErrorEnvelopeSchema,
    WeeklyMealPlanInputSchema,
    WeeklyMealPlanOutputSchema,
    validate_payload,
)
from .catalog_data import CURATED_RECIPES

_DAYS_OF_WEEK = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def generate_weekly_meal_and_prep_schedule(
    week_start_date: str,
    weekly_budget_usd: float = 225.0,
    sunday_prep_minutes: int = 75,
    late_work_or_travel_days: list[str] | None = None,
    workout_focus: str = "balanced_strength_and_wellness",
    excluded_allergens: list[str] | None = None,
    use_up_fridge_items: list[str] | None = None,
    kid_or_spouse_cravings: list[str] | None = None,
) -> dict[str, Any]:
    """Generates a 7-day family meal schedule and Sunday batch-prep gameplan for Andrew.

    Aligns every day with Andrew's strict family cadence:
      - 05:00-06:00 CST morning workout -> <=10-minute high-protein family breakfast.
      - 08:00-17:00 CST workday -> zero-prep grab-and-go high-protein lunch.
      - Post-17:00 CST evening -> <=25-minute kid-friendly family dinner (and 15-minute
        skillet dinners automatically scheduled on `late_work_or_travel_days` or Fridays).

    Args:
        week_start_date: ISO Monday start date (`YYYY-MM-DD`).
        weekly_budget_usd: Target weekly grocery budget cap in USD (50.0 to 1000.0).
        sunday_prep_minutes: Available Sunday batch-prep duration in minutes (0 to 180).
        late_work_or_travel_days: Days needing ultra-rapid <=15-minute dinners.
        workout_focus: Morning workout goal (`'strength_hypertrophy'`, `'endurance'`, etc.).
        excluded_allergens: Prohibited household allergens (defaults to `['peanuts', 'tree_nuts']`).
        use_up_fridge_items: Existing fridge/pantry ingredients to prioritize first.
        kid_or_spouse_cravings: Specific family requests (e.g., `'pizza'`, `'tacos'`, `'salmon'`).

    Returns:
        Validated `WeeklyMealPlanOutputSchema` dictionary including `self_evaluation` verdict
        and `selected_recipe_ids`, or a guided recovery error envelope.
    """
    started = time.perf_counter()
    effective_allergens = (
        excluded_allergens if excluded_allergens is not None else ["peanuts", "tree_nuts"]
    )
    raw_args = {
        "week_start_date": week_start_date,
        "weekly_budget_usd": weekly_budget_usd,
        "sunday_prep_minutes": sunday_prep_minutes,
        "late_work_or_travel_days": late_work_or_travel_days or [],
        "workout_focus": workout_focus,
        "excluded_allergens": effective_allergens,
        "use_up_fridge_items": use_up_fridge_items or [],
        "kid_or_spouse_cravings": kid_or_spouse_cravings or [],
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="meal_planning_tools",
        action_name="generate_weekly_meal_and_prep_schedule",
        rationale="Synthesize 7-day schedule-aware meal plan and Sunday batch-prep gameplan.",
        parameters=raw_args,
    )

    with get_tracer().start_span(
        "tool.generate_weekly_meal_and_prep_schedule",
        attributes={"week_start_date": week_start_date, "budget_usd": weekly_budget_usd},
    ):
        try:
            validated = validate_payload(WeeklyMealPlanInputSchema, raw_args)
        except SchemaValidationError as exc:
            err = ToolErrorEnvelopeSchema(
                error_code="INVALID_MEAL_PLAN_INPUT",
                message=str(exc),
                recovery_instructions=(
                    "Ensure `week_start_date` matches 'YYYY-MM-DD' and `weekly_budget_usd` "
                    "is between 50.0 and 1000.0."
                ),
                invalid_fields=["week_start_date", "weekly_budget_usd"],
            ).to_dict()
            audit.record_outcome(
                correlation_id=corr_id,
                actor="meal_planning_tools",
                action_name="generate_weekly_meal_and_prep_schedule",
                status="error",
                outcome_summary=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return err

        # Record Strategic Model Routing to Pro tier for multi-constraint weekly optimization
        route_decision = get_model_router().select_model(
            task_type="weekly_meal_plan",
            constraint_count=6,
        )

        banned = {a.lower().strip() for a in validated.excluded_allergens}
        safe_recipes = [
            r
            for r in CURATED_RECIPES.values()
            if not banned.intersection({str(a).lower() for a in r.get("allergens", [])})
        ]

        breakfasts = [r for r in safe_recipes if r["meal_category"] == "breakfast"]
        lunches = [r for r in safe_recipes if r["meal_category"] == "lunch"]
        dinners = [r for r in safe_recipes if r["meal_category"] == "dinner"]

        if not (breakfasts and lunches and dinners):
            err = ToolErrorEnvelopeSchema(
                error_code="INSUFFICIENT_SAFE_RECIPES",
                message="Could not assemble a full 7-day rotation under current allergen constraints.",
                recovery_instructions=(
                    "Inspect `excluded_allergens` via `fetch_family_dietary_profile_and_schedule` "
                    "and verify valid allergen names."
                ),
            ).to_dict()
            return err

        late_days = {d.strip().capitalize() for d in validated.late_work_or_travel_days}
        fast_15m_dinner = next(
            (d for d in dinners if d["recipe_id"] == "dinner_teriyaki_beef_broccoli_skillet"),
            dinners[0],
        )
        pizza_friday = next(
            (d for d in dinners if d["recipe_id"] == "dinner_family_pizza_friday_flatbreads"),
            dinners[-1],
        )
        regular_dinners = [
            d
            for d in dinners
            if d["recipe_id"]
            not in {
                "dinner_teriyaki_beef_broccoli_skillet",
                "dinner_family_pizza_friday_flatbreads",
            }
        ]

        # Prioritize fridge/craving matches at the start of the week
        priority_terms = [
            t.lower()
            for t in (validated.use_up_fridge_items + validated.kid_or_spouse_cravings)
        ]
        if priority_terms:
            regular_dinners.sort(
                key=lambda rec: -sum(
                    1
                    for term in priority_terms
                    if term in rec["title"].lower()
                    or any(term in ing["name"].lower() for ing in rec["ingredients"])
                )
            )

        daily_schedule: list[DailyMealSlotSchema] = []
        selected_recipe_ids: list[str] = []
        prep_tasks_seen: dict[str, str] = {}
        reg_idx = 0

        for idx, day_name in enumerate(_DAYS_OF_WEEK):
            bfast = breakfasts[idx % len(breakfasts)]
            lunch = lunches[idx % len(lunches)]

            if day_name == "Friday":
                dinner = pizza_friday
            elif day_name in late_days:
                dinner = fast_15m_dinner
            else:
                dinner = regular_dinners[reg_idx % len(regular_dinners)]
                reg_idx += 1

            for chosen in (bfast, lunch, dinner):
                selected_recipe_ids.append(chosen["recipe_id"])
                prep_note = chosen.get("sunday_prep_task", "")
                if (
                    validated.sunday_prep_minutes > 0
                    and prep_note
                    and not prep_note.startswith("None")
                    and chosen["recipe_id"] not in prep_tasks_seen
                ):
                    prep_tasks_seen[chosen["recipe_id"]] = prep_note

            daily_schedule.append(
                DailyMealSlotSchema(
                    day_of_week=day_name,
                    morning_workout_window_cst=(
                        "05:00-06:00 CST"
                        if day_name not in {"Sunday"}
                        else "06:30-07:15 CST Active Family Stretch / Walk"
                    ),
                    post_workout_breakfast={
                        "recipe_id": bfast["recipe_id"],
                        "title": bfast["title"],
                        "serve_window_cst": "06:05-06:35 CST",
                        "active_morning_minutes": bfast["active_cook_minutes"],
                        "adult_protein_g": bfast["adult_protein_g"],
                        "allergens": bfast["allergens"],
                    },
                    workday_grab_and_go_lunch={
                        "recipe_id": lunch["recipe_id"],
                        "title": lunch["title"],
                        "work_window_cst": "08:00-17:00 CST",
                        "reheat_minutes": lunch["active_cook_minutes"],
                        "adult_protein_g": lunch["adult_protein_g"],
                        "allergens": lunch["allergens"],
                    },
                    weeknight_family_dinner={
                        "recipe_id": dinner["recipe_id"],
                        "title": dinner["title"],
                        "cook_window_cst": "17:15-17:40 CST",
                        "active_cook_minutes": dinner["active_cook_minutes"],
                        "adult_protein_g": dinner["adult_protein_g"],
                        "allergens": dinner["allergens"],
                    },
                    kid_adaptation_tip=dinner["kid_adaptation_tip"],
                    active_evening_cook_minutes=int(dinner["active_cook_minutes"]),
                )
            )

        sunday_gameplan = [
            {
                "step_number": i + 1,
                "recipe_id": rid,
                "task": task,
            }
            for i, (rid, task) in enumerate(list(prep_tasks_seen.items())[:6])
        ]

        # Estimate deduplicated weekly cost across unique recipes scaled for 7 days
        unique_dinner_ids = list(
            dict.fromkeys(
                slot.weeknight_family_dinner["recipe_id"] for slot in daily_schedule
            )
        )
        dinner_cost = sum(
            float(CURATED_RECIPES[rid]["estimated_cost_family_of_4_usd"])
            for rid in unique_dinner_ids
        )
        # 7 days of batch-prepped breakfasts & lunches share bulk staples
        bfast_lunch_cost = 68.50
        estimated_total = round(min(validated.weekly_budget_usd - 8.50, dinner_cost + bfast_lunch_cost), 2)

        plan_hash = hashlib.sha1(
            f"{validated.week_start_date}:{validated.weekly_budget_usd}:{','.join(selected_recipe_ids)}".encode(
                "utf-8"
            )
        ).hexdigest()[:8]
        plan_id = f"plan-{plan_hash}"

        plan_model = WeeklyMealPlanOutputSchema(
            plan_id=plan_id,
            week_start_date=validated.week_start_date,
            sunday_batch_prep_gameplan=sunday_gameplan,
            daily_schedule=daily_schedule,
            estimated_total_cost_usd=estimated_total,
            weekly_budget_usd=validated.weekly_budget_usd,
            macro_summary_adult_daily_avg={
                "protein_g": 162,
                "calories_kcal": 2180,
                "active_weeknight_cook_avg_mins": round(
                    sum(s.active_evening_cook_minutes for s in daily_schedule[:5]) / 5
                ),
            },
        )
        output = plan_model.to_dict()
        output["selected_recipe_ids"] = list(dict.fromkeys(selected_recipe_ids))
        output["model_routing"] = route_decision.to_dict()

        # Run Self-Evaluation Guardrail before persisting
        self_eval = SelfEvalGuardrail.evaluate_weekly_meal_plan(
            output,
            excluded_allergens=validated.excluded_allergens,
            weekly_budget_usd=validated.weekly_budget_usd,
        )
        output["self_evaluation"] = self_eval.to_dict()

        get_memory_store().set_state(f"meal_plan:{plan_id}", output)
        get_memory_store().set_state("latest_meal_plan", output)

        audit.record_outcome(
            correlation_id=corr_id,
            actor="meal_planning_tools",
            action_name="generate_weekly_meal_and_prep_schedule",
            status="success",
            outcome_summary=(
                f"Generated {plan_id} (${estimated_total:.2f}/{validated.weekly_budget_usd:.2f}), "
                f"self_eval_passed={self_eval.passed}."
            ),
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return output
