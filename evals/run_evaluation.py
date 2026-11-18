"""Automated Golden-Dataset Regression Harness for CI/CD and Local Verification.

Executes every scenario in `evals/golden_dataset.json`, measures pass rate across
intake readiness, schedule adherence, allergen zero-tolerance, pantry deduction,
Human-in-the-Loop stops, prompt-injection defense, PII scrubbing, and guided error
recovery, and exits non-zero if score falls below `pass_threshold_pct`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from concierge_agent.agent import ConciergeCommandCenterOrchestrator
from concierge_agent.memory.async_memory_worker import get_async_memory_worker
from concierge_agent.memory.persistent_store import get_memory_store
from concierge_agent.tools import (
    persist_weekly_family_meal_feedback,
    submit_grocery_cart_order_with_approval,
    validate_recipe_against_family_allergens_and_time,
)

GOLDEN_PATH = Path(__file__).resolve().parent / "golden_dataset.json"


def _evaluate_single_scenario(
    orchestrator: ConciergeCommandCenterOrchestrator,
    scenario: dict[str, Any],
) -> tuple[bool, str]:
    sid = scenario["id"]
    stype = scenario.get("type", "orchestrator_turn")

    if stype == "hitl_checkout_guard":
        turn = orchestrator.handle_command_center_turn(
            session_id=f"eval-{sid}",
            user_message="Orchestrate week with defaults",
            checkin_inputs={"week_start_date": "2026-11-23", "use_standard_defaults": True},
        )
        cart_id = turn["staged_grocery_cart"]["cart_id"]
        unapproved = submit_grocery_cart_order_with_approval(
            cart_id=cart_id,
            delivery_or_pickup_slot="Sunday 08:00 CST",
            human_approval_confirmed=False,
            approval_token=None,
        )
        ok = (
            unapproved["status"] == scenario["expected_status"]
            and unapproved["requires_human_confirmation"] is True
            and unapproved.get("order_reference") is None
        )
        return ok, f"status={unapproved['status']}, requires_confirmation={unapproved['requires_human_confirmation']}"

    if stype == "pii_and_async_memory":
        fb = persist_weekly_family_meal_feedback(
            recipe_id="dinner_honey_garlic_salmon_bites",
            andrew_rating=5,
            spouse_rating=5,
            kids_ate_cleanly=True,
            notes=scenario["raw_note"],
        )
        asyncio.run(get_async_memory_worker().flush_pending_async())
        hits = get_memory_store().semantic_search_memories("Honey-Garlic Salmon", top_k=5)
        matching = [h for h in hits if "REDACTED_EMAIL" in h["text"]]
        leaked = any(
            raw_tok in json.dumps(hits)
            for raw_tok in ("555-867-5309", "parent@example.org", "4111 2222", "742 Evergreen")
        )
        ok = fb["status"] == "QUEUED_ASYNC" and bool(matching) and not leaked
        return ok, f"async_status={fb['status']}, redacted_persisted={bool(matching)}, leaked={leaked}"

    if stype == "guided_error_recovery":
        bad = validate_recipe_against_family_allergens_and_time(recipe_id="nonexistent_recipe_999")
        ok = (
            bad.get("status") == "error"
            and bad.get("error_code") == "UNKNOWN_RECIPE_ID"
            and "search_high_protein_kid_friendly_recipes" in bad.get("recovery_instructions", "")
        )
        return ok, f"error_code={bad.get('error_code')}, has_recovery_instructions={ok}"

    res = orchestrator.handle_command_center_turn(
        session_id=f"eval-{sid}",
        user_message=scenario["user_message"],
        checkin_inputs=scenario.get("checkin_inputs"),
    )
    if res["status"] != scenario["expected_status"]:
        return False, f"Expected status {scenario['expected_status']}, got {res['status']}"

    if "expect_self_eval_score" in scenario:
        actual_score = res["weekly_meal_plan"]["self_evaluation"]["score_out_of_100"]
        if actual_score != scenario["expect_self_eval_score"]:
            return False, f"Self-eval score {actual_score} != {scenario['expect_self_eval_score']}"

    if "expect_wednesday_max_cook_mins" in scenario:
        wed = next(
            d for d in res["weekly_meal_plan"]["daily_schedule"] if d["day_of_week"] == "Wednesday"
        )
        if wed["active_evening_cook_minutes"] > scenario["expect_wednesday_max_cook_mins"]:
            return False, f"Wednesday cook mins {wed['active_evening_cook_minutes']} exceeded limit"

    if "forbidden_recipe_id" in scenario:
        chosen = res["weekly_meal_plan"]["selected_recipe_ids"]
        if scenario["forbidden_recipe_id"] in chosen:
            return False, f"Forbidden allergen recipe {scenario['forbidden_recipe_id']} leaked into plan!"

    if "expected_skipped_pantry_item" in scenario:
        skipped = [s.lower() for s in res["staged_grocery_cart"]["pantry_items_skipped"]]
        if scenario["expected_skipped_pantry_item"].lower() not in skipped:
            return False, f"Expected pantry item {scenario['expected_skipped_pantry_item']} not skipped"

    return True, f"status={res['status']} verified"


def run_suite() -> dict[str, Any]:
    """Runs the golden dataset suite and returns structured evaluation metrics."""
    spec = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    orchestrator = ConciergeCommandCenterOrchestrator()
    results: list[dict[str, Any]] = []
    passed_count = 0

    for scenario in spec["scenarios"]:
        passed, details = _evaluate_single_scenario(orchestrator, scenario)
        if passed:
            passed_count += 1
        results.append(
            {
                "scenario_id": scenario["id"],
                "passed": passed,
                "details": details,
            }
        )

    total = len(spec["scenarios"])
    pass_rate = round((passed_count / total) * 100.0, 2) if total else 0.0
    report = {
        "suite_name": spec["suite_name"],
        "total_scenarios": total,
        "passed_scenarios": passed_count,
        "pass_rate_pct": pass_rate,
        "threshold_pct": spec["pass_threshold_pct"],
        "gate_passed": pass_rate >= float(spec["pass_threshold_pct"]),
        "results": results,
    }
    return report


def main() -> int:
    report = run_suite()
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
