"""Smart Aisle-Sorted Grocery Consolidation & HITL Checkout Tools."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ..config.secrets import get_secret_provider
from ..config.settings import get_settings
from ..memory.persistent_store import get_memory_store
from ..observability.structured_logger import get_audit_logger
from ..observability.tracing import get_tracer
from ..orchestration.hitl_hooks import get_approval_gate
from ..schemas.models import (
    GroceryCartItemSchema,
    GroceryConsolidationInputSchema,
    GroceryConsolidationOutputSchema,
    GroceryOrderSubmissionInputSchema,
    GroceryOrderSubmissionOutputSchema,
    SchemaValidationError,
    ToolErrorEnvelopeSchema,
    validate_payload,
)
from .catalog_data import CURATED_RECIPES


def consolidate_aisle_sorted_grocery_list(
    plan_id: str,
    recipe_ids: list[str],
    household_size_adults: int = 2,
    household_size_kids: int = 2,
    subtract_pantry_inventory: bool = True,
    weekly_budget_usd: float = 225.0,
) -> dict[str, Any]:
    """Consolidates multi-day recipe ingredients, subtracts pantry staples, and groups by aisle.

    Automatically stages the consolidated cart in `PENDING_HUMAN_APPROVAL` status via
    `HumanApprovalGate` so Andrew can review quantities, pantry deductions, and subtotal
    before any order is placed.

    Args:
        plan_id: Identifier of the weekly meal plan (`plan-...`).
        recipe_ids: Non-empty list of catalog `recipe_id` strings included in the week's plan.
        household_size_adults: Number of adults in the household (default 2: Andrew + spouse).
        household_size_kids: Number of young children in the household (default 2).
        subtract_pantry_inventory: When True, deducts items already stocked in Andrew's pantry.
        weekly_budget_usd: Budget ceiling in USD used to flag `within_budget`.

    Returns:
        Validated `GroceryConsolidationOutputSchema` dictionary including `approval_token`
        for HITL confirmation, or a guided recovery error envelope.
    """
    started = time.perf_counter()
    raw_args = {
        "plan_id": plan_id,
        "recipe_ids": recipe_ids,
        "household_size_adults": household_size_adults,
        "household_size_kids": household_size_kids,
        "subtract_pantry_inventory": subtract_pantry_inventory,
        "weekly_budget_usd": weekly_budget_usd,
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="grocery_cart_tools",
        action_name="consolidate_aisle_sorted_grocery_list",
        rationale="Deduplicate weekly ingredients, subtract pantry stock, and stage HITL cart.",
        parameters=raw_args,
    )

    with get_tracer().start_span(
        "tool.consolidate_aisle_sorted_grocery_list",
        attributes={"plan_id": plan_id, "recipe_count": len(recipe_ids or [])},
    ):
        try:
            validated = validate_payload(GroceryConsolidationInputSchema, raw_args)
        except SchemaValidationError as exc:
            err = ToolErrorEnvelopeSchema(
                error_code="INVALID_GROCERY_CONSOLIDATION_INPUT",
                message=str(exc),
                recovery_instructions=(
                    "Provide a valid `plan_id` (from `generate_weekly_meal_and_prep_schedule`) "
                    "and a non-empty list of valid `recipe_ids`."
                ),
                invalid_fields=["plan_id", "recipe_ids"],
            ).to_dict()
            audit.record_outcome(
                correlation_id=corr_id,
                actor="grocery_cart_tools",
                action_name="consolidate_aisle_sorted_grocery_list",
                status="error",
                outcome_summary=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return err

        unknown_ids = [rid for rid in validated.recipe_ids if rid not in CURATED_RECIPES]
        if unknown_ids:
            err = ToolErrorEnvelopeSchema(
                error_code="UNKNOWN_RECIPE_IDS_IN_CART",
                message=f"Unknown recipe_ids cannot be consolidated: {unknown_ids}",
                recovery_instructions=(
                    "Remove unknown IDs or call `search_high_protein_kid_friendly_recipes` "
                    "to retrieve valid catalog `recipe_id` values."
                ),
                invalid_fields=["recipe_ids"],
            ).to_dict()
            return err

        store = get_memory_store()
        profile = store.get_state("family_profile", default={})
        pantry_map: dict[str, Any] = (
            profile.get("pantry_inventory", {}) if validated.subtract_pantry_inventory else {}
        )

        merged: dict[str, dict[str, Any]] = {}
        pantry_skipped: list[str] = []

        for rid in dict.fromkeys(validated.recipe_ids):
            rec = CURATED_RECIPES[rid]
            for ing in rec["ingredients"]:
                key = ing["name"].lower()
                if key in pantry_map:
                    if ing["name"] not in pantry_skipped:
                        pantry_skipped.append(ing["name"])
                    continue
                if key not in merged:
                    merged[key] = {
                        "ingredient_name": ing["name"],
                        "aisle_category": ing["aisle"],
                        "total_quantity": float(ing["qty"]),
                        "unit": ing["unit"],
                        "pantry_deducted_quantity": 0.0,
                        "estimated_cost_usd": float(ing["cost"]),
                        "used_in_meals": [rec["title"]],
                    }
                else:
                    merged[key]["total_quantity"] = round(
                        merged[key]["total_quantity"] + float(ing["qty"]), 2
                    )
                    merged[key]["estimated_cost_usd"] = round(
                        merged[key]["estimated_cost_usd"] + float(ing["cost"]), 2
                    )
                    if rec["title"] not in merged[key]["used_in_meals"]:
                        merged[key]["used_in_meals"].append(rec["title"])

        aisle_groups: dict[str, list[GroceryCartItemSchema]] = {}
        subtotal = 0.0
        for item_data in merged.values():
            subtotal += item_data["estimated_cost_usd"]
            aisle = item_data["aisle_category"]
            aisle_groups.setdefault(aisle, []).append(GroceryCartItemSchema(**item_data))

        subtotal = round(subtotal, 2)
        cart_hash = hashlib.sha1(
            f"{validated.plan_id}:{subtotal}".encode("utf-8")
        ).hexdigest()[:8]
        cart_id = f"cart-{cart_hash}"

        gate_record = get_approval_gate().stage_for_approval(
            action_type="grocery_order_checkout",
            target_id=cart_id,
            summary=(
                f"Dispatch grocery cart {cart_id} ({len(merged)} items, ${subtotal:.2f} estimated)"
            ),
            estimated_cost_usd=subtotal,
            payload={"plan_id": validated.plan_id},
        )

        cart_output = GroceryConsolidationOutputSchema(
            cart_id=cart_id,
            approval_status="PENDING_HUMAN_APPROVAL",
            total_items=len(merged),
            pantry_items_skipped=sorted(pantry_skipped),
            estimated_subtotal_usd=subtotal,
            within_budget=subtotal <= validated.weekly_budget_usd,
            aisle_grouped_items=aisle_groups,
        ).to_dict()
        cart_output["approval_token"] = gate_record["approval_token"]

        store.set_state(f"grocery_cart:{cart_id}", cart_output)
        store.set_state("latest_grocery_cart", cart_output)

        audit.record_outcome(
            correlation_id=corr_id,
            actor="grocery_cart_tools",
            action_name="consolidate_aisle_sorted_grocery_list",
            status="success",
            outcome_summary=(
                f"Staged {cart_id} with {len(merged)} items (${subtotal:.2f}), "
                f"skipped {len(pantry_skipped)} pantry staples."
            ),
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return cart_output


def submit_grocery_cart_order_with_approval(
    cart_id: str,
    delivery_or_pickup_slot: str,
    human_approval_confirmed: bool = False,
    approval_token: str | None = None,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Submits a staged grocery order ONLY after explicit Human-in-the-Loop approval from Andrew.

    High-stakes financial action protected by `HumanApprovalGate` and Google Cloud Secret
    Manager credential injection (`GROCERY_PROVIDER_API_SECRET_ID`). If called without a
    verified human approval token, execution halts safely with `PENDING_HUMAN_APPROVAL`.

    Args:
        cart_id: Identifier of the staged grocery cart (`cart-...`).
        delivery_or_pickup_slot: Preferred pickup/delivery window (e.g. `'Sunday 07:30-08:30 CST Curbside'`).
        human_approval_confirmed: Boolean flag indicating Andrew explicitly approved checkout.
        approval_token: One-time HITL token granted when Andrew clicks Approve.
        tool_context: Optional ADK `ToolContext` used to pause execution natively in ADK Runner.

    Returns:
        `GroceryOrderSubmissionOutputSchema` dictionary with `status='PENDING_HUMAN_APPROVAL'`
        when awaiting confirmation, or `status='ORDER_DISPATCHED_ISOLATED'` once approved.
    """
    started = time.perf_counter()
    raw_args = {
        "cart_id": cart_id,
        "delivery_or_pickup_slot": delivery_or_pickup_slot,
        "human_approval_confirmed": human_approval_confirmed,
        "approval_token": approval_token,
    }
    audit = get_audit_logger()
    corr_id = audit.record_intent(
        actor="grocery_cart_tools",
        action_name="submit_grocery_cart_order_with_approval",
        rationale="Attempt grocery cart checkout guarded by Human-in-the-Loop confirmation.",
        parameters=raw_args,
    )

    with get_tracer().start_span(
        "tool.submit_grocery_cart_order_with_approval",
        attributes={"cart_id": cart_id, "human_approval_confirmed": human_approval_confirmed},
    ):
        try:
            validated = validate_payload(GroceryOrderSubmissionInputSchema, raw_args)
        except SchemaValidationError as exc:
            return ToolErrorEnvelopeSchema(
                error_code="INVALID_GROCERY_ORDER_ARGS",
                message=str(exc),
                recovery_instructions=(
                    "Pass a valid `cart_id` from `consolidate_aisle_sorted_grocery_list` and "
                    "a descriptive `delivery_or_pickup_slot` (at least 5 characters)."
                ),
                invalid_fields=["cart_id", "delivery_or_pickup_slot"],
            ).to_dict()

        staged_cart = get_memory_store().get_state(f"grocery_cart:{validated.cart_id}")
        if staged_cart is None:
            return ToolErrorEnvelopeSchema(
                error_code="CART_NOT_FOUND",
                message=f"Grocery cart '{validated.cart_id}' does not exist in session state.",
                recovery_instructions=(
                    "Call `consolidate_aisle_sorted_grocery_list` first to stage a valid cart "
                    "before attempting checkout."
                ),
                invalid_fields=["cart_id"],
            ).to_dict()

        authorized, pending_gate = get_approval_gate().verify_execution_authorized(
            action_type="grocery_order_checkout",
            target_id=validated.cart_id,
            human_approval_confirmed=validated.human_approval_confirmed,
            approval_token=validated.approval_token,
            tool_context=tool_context,
        )
        if not authorized and pending_gate is not None:
            paused = GroceryOrderSubmissionOutputSchema(
                status="PENDING_HUMAN_APPROVAL",
                cart_id=validated.cart_id,
                order_reference=None,
                requires_human_confirmation=True,
                confirmation_prompt=(
                    f"Action paused for Andrew's approval: Confirm grocery checkout for "
                    f"{validated.cart_id} (${staged_cart['estimated_subtotal_usd']:.2f}) "
                    f"scheduled for '{validated.delivery_or_pickup_slot}'. "
                    f"(Approval Token: {pending_gate['approval_token']})"
                ),
            ).to_dict()
            paused["approval_token"] = pending_gate["approval_token"]
            audit.record_outcome(
                correlation_id=corr_id,
                actor="grocery_cart_tools",
                action_name="submit_grocery_cart_order_with_approval",
                status="PENDING_HUMAN_APPROVAL",
                outcome_summary="HITL hard-stop enforced; awaiting Andrew's confirmation.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            return paused

        # Retrieve external grocery partner credential strictly via Secret Manager (never hardcoded)
        settings = get_settings()
        _api_credential = get_secret_provider().get_secret(settings.grocery_api_secret_id)
        del _api_credential  # Never log or echo resolved secrets

        order_ref = f"ORD-{validated.cart_id.upper()}-CONFIRMED"
        staged_cart["approval_status"] = "APPROVED_AND_DISPATCHED"
        staged_cart["order_reference"] = order_ref
        staged_cart["delivery_or_pickup_slot"] = validated.delivery_or_pickup_slot
        get_memory_store().set_state(f"grocery_cart:{validated.cart_id}", staged_cart)
        get_memory_store().set_state("latest_grocery_cart", staged_cart)

        confirmed = GroceryOrderSubmissionOutputSchema(
            status="ORDER_DISPATCHED_ISOLATED",
            cart_id=validated.cart_id,
            order_reference=order_ref,
            requires_human_confirmation=False,
            confirmation_prompt=None,
        ).to_dict()

        audit.record_outcome(
            correlation_id=corr_id,
            actor="grocery_cart_tools",
            action_name="submit_grocery_cart_order_with_approval",
            status="ORDER_DISPATCHED_ISOLATED",
            outcome_summary=f"Order {order_ref} dispatched after verified human approval.",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        return confirmed
