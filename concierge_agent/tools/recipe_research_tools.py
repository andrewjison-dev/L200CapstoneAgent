"""Recipe Research & Safety Validation Tools for Andrew's Family Concierge Agent."""

from __future__ import annotations

import time
from typing import Any

from ..observability.structured_logger import get_audit_logger
from ..observability.tracing import get_tracer
from ..schemas.models import (
    RecipeSearchInputSchema,
    RecipeSearchOutputSchema,
    SchemaValidationError,
    ToolErrorEnvelopeSchema,
    validate_payload,
)
from .catalog_data import CURATED_RECIPES


def search_high_protein_kid_friendly_recipes(
    meal_category: str = "all",
    max_active_cook_minutes: int = 25,
    min_protein_grams_per_adult_serving: int = 28,
    excluded_allergens: list[str] | None = None,
    prefer_ingredients: list[str] | None = None,
) -> dict[str, Any]:
    """Searches curated family recipes matching post-5AM-workout protein, cook-time, and allergen rules.

    Filters recipes so breakfasts assemble quickly after Andrew's 05:00-06:00 CST workout,
    lunches require zero midday cooking during his 08:00-17:00 CST workday, and weeknight
    dinners finish in <=25-30 minutes with kid-friendly split-seasoning adaptations.

    Args:
        meal_category: Target slot to search (`'breakfast'`, `'lunch'`, `'dinner'`, or `'all'`).
        max_active_cook_minutes: Maximum active kitchen prep/cook time in minutes (5 to 60).
        min_protein_grams_per_adult_serving: Minimum protein per adult portion in grams (10 to 80).
        excluded_allergens: Household allergens to strictly exclude (defaults to `['peanuts', 'tree_nuts']`).
        prefer_ingredients: Optional list of fridge/pantry items to boost to the top of results.

    Returns:
        Validated `RecipeSearchOutputSchema` dictionary with matching recipes ranked by
        fridge-ingredient overlap and kid-friendly score, or a guided error envelope.
    """
    started = time.perf_counter()
    effective_allergens = (
        excluded_allergens if excluded_allergens is not None else ["peanuts", "tree_nuts"]
    )
    raw_args = {
        "meal_category": meal_category,
        "max_active_cook_minutes": max_active_cook_minutes,
        "min_protein_grams_per_adult_serving": min_protein_grams_per_adult_serving,
        "excluded_allergens": effective_allergens,
        "prefer_ingredients": prefer_ingredients or [],
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="recipe_research_tools",
        action_name="search_high_protein_kid_friendly_recipes",
        rationale="Search allergen-safe, fast, high-protein family recipes.",
        parameters=raw_args,
    )

    with get_tracer().start_span(
        "tool.search_high_protein_kid_friendly_recipes",
        attributes={"meal_category": meal_category, "max_cook_mins": max_active_cook_minutes},
    ):
        try:
            validated = validate_payload(RecipeSearchInputSchema, raw_args)
        except SchemaValidationError as exc:
            err = ToolErrorEnvelopeSchema(
                error_code="INVALID_RECIPE_SEARCH_PARAMS",
                message=str(exc),
                recovery_instructions=(
                    "Set `meal_category` to one of 'breakfast', 'lunch', 'dinner', or 'all', "
                    "`max_active_cook_minutes` between 5 and 60, and "
                    "`min_protein_grams_per_adult_serving` between 10 and 80."
                ),
                invalid_fields=["meal_category", "max_active_cook_minutes"],
            ).to_dict()
            audit.record_outcome(
                correlation_id=corr_id,
                actor="recipe_research_tools",
                action_name="search_high_protein_kid_friendly_recipes",
                status="error",
                outcome_summary=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return err

        banned = {a.lower().strip() for a in validated.excluded_allergens}
        preferred_terms = [p.lower().strip() for p in validated.prefer_ingredients if p.strip()]

        matches: list[dict[str, Any]] = []
        for recipe in CURATED_RECIPES.values():
            if (
                validated.meal_category != "all"
                and recipe["meal_category"] != validated.meal_category
            ):
                continue
            if int(recipe["active_cook_minutes"]) > validated.max_active_cook_minutes:
                continue
            if int(recipe["adult_protein_g"]) < validated.min_protein_grams_per_adult_serving:
                continue

            recipe_allergens = {str(a).lower() for a in recipe.get("allergens", [])}
            if banned.intersection(recipe_allergens):
                continue

            ing_blob = " ".join(i["name"].lower() for i in recipe["ingredients"])
            overlap_hits = sum(1 for term in preferred_terms if term in ing_blob)

            enriched = dict(recipe)
            enriched["fridge_match_hits"] = overlap_hits
            matches.append(enriched)

        matches.sort(
            key=lambda r: (
                -int(r["fridge_match_hits"]),
                -int(r["kid_friendly_score"]),
                int(r["active_cook_minutes"]),
            )
        )

        if not matches:
            err = ToolErrorEnvelopeSchema(
                error_code="NO_RECIPES_MATCHED_FILTERS",
                message=(
                    f"Zero recipes matched category='{validated.meal_category}' with "
                    f"<= {validated.max_active_cook_minutes} mins and >= "
                    f"{validated.min_protein_grams_per_adult_serving}g protein."
                ),
                recovery_instructions=(
                    "Relax `max_active_cook_minutes` up to 30 or lower "
                    "`min_protein_grams_per_adult_serving` to 25 while keeping `excluded_allergens` intact."
                ),
            ).to_dict()
            audit.record_outcome(
                correlation_id=corr_id,
                actor="recipe_research_tools",
                action_name="search_high_protein_kid_friendly_recipes",
                status="error",
                outcome_summary="Zero matching recipes.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return err

        out = RecipeSearchOutputSchema(
            candidate_count=len(matches),
            recipes=matches,
        ).to_dict()
        audit.record_outcome(
            correlation_id=corr_id,
            actor="recipe_research_tools",
            action_name="search_high_protein_kid_friendly_recipes",
            status="success",
            outcome_summary=f"Found {len(matches)} safe recipes.",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return out


def validate_recipe_against_family_allergens_and_time(
    recipe_id: str,
    excluded_allergens: list[str] | None = None,
    max_weeknight_minutes: int = 30,
) -> dict[str, Any]:
    """Validates a specific `recipe_id` against household allergens and weeknight time limits.

    Args:
        recipe_id: Identifier of the candidate recipe in the catalog.
        excluded_allergens: List of prohibited allergens (defaults to `['peanuts', 'tree_nuts']`).
        max_weeknight_minutes: Maximum allowed active evening cook time in minutes.

    Returns:
        Dictionary with `status`, `is_safe_and_compliant`, `violations`, and recipe metadata,
        or a guided recovery error envelope if `recipe_id` does not exist.
    """
    started = time.perf_counter()
    banned = {
        a.lower().strip()
        for a in (excluded_allergens if excluded_allergens is not None else ["peanuts", "tree_nuts"])
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="recipe_research_tools",
        action_name="validate_recipe_against_family_allergens_and_time",
        rationale=f"Verify safety and cook-time compliance for recipe '{recipe_id}'.",
        parameters={
            "recipe_id": recipe_id,
            "excluded_allergens": sorted(banned),
            "max_weeknight_minutes": max_weeknight_minutes,
        },
    )

    with get_tracer().start_span(
        "tool.validate_recipe_against_family_allergens_and_time",
        attributes={"recipe_id": recipe_id},
    ):
        recipe = CURATED_RECIPES.get(recipe_id)
        if recipe is None:
            err = ToolErrorEnvelopeSchema(
                error_code="UNKNOWN_RECIPE_ID",
                message=f"Recipe ID '{recipe_id}' was not found in the curated family catalog.",
                recovery_instructions=(
                    "Call `search_high_protein_kid_friendly_recipes(meal_category='all')` first "
                    "and select a valid `recipe_id` from the returned `recipes` list."
                ),
                invalid_fields=["recipe_id"],
            ).to_dict()
            audit.record_outcome(
                correlation_id=corr_id,
                actor="recipe_research_tools",
                action_name="validate_recipe_against_family_allergens_and_time",
                status="error",
                outcome_summary=f"Unknown recipe_id '{recipe_id}'.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return err

        violations: list[str] = []
        present_allergens = {str(a).lower() for a in recipe.get("allergens", [])}
        overlap = sorted(banned.intersection(present_allergens))
        if overlap:
            violations.append(f"Contains prohibited household allergens: {overlap}")

        if int(recipe["active_cook_minutes"]) > max_weeknight_minutes:
            violations.append(
                f"Active cook time ({recipe['active_cook_minutes']}m) exceeds {max_weeknight_minutes}m limit."
            )

        compliant = len(violations) == 0
        res = {
            "status": "success",
            "recipe_id": recipe_id,
            "title": recipe["title"],
            "is_safe_and_compliant": compliant,
            "violations": violations,
            "kid_adaptation_tip": recipe["kid_adaptation_tip"],
        }
        audit.record_outcome(
            correlation_id=corr_id,
            actor="recipe_research_tools",
            action_name="validate_recipe_against_family_allergens_and_time",
            status="success",
            outcome_summary=f"compliant={compliant}, violations={len(violations)}",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return res
