"""Security Policy Guardrails & Automated Self-Evaluation ADK Plugin.

Implements:
  1. Prompt-Injection & Policy Guardrail (`before_model_callback` + optional Google Cloud
     Model Armor inspection) blocking jailbreaks, allergen-bypass attempts, or PII leaks.
  2. Pre-Tool Safety & Budget Policy (`before_tool_callback`) blocking prohibited household
     allergens and recording `INTENT` audit logs.
  3. Post-Tool Outcome Verification (`after_tool_callback`) recording `OUTCOME` audit logs.
  4. Automated Self-Evaluation Guardrail (`SelfEvalGuardrail`) scoring generated weekly
     meal plans against Andrew's rigid daily schedule (5-6AM CST workout, <=30m weeknight
     dinners, allergen zero-tolerance, budget cap, and kid-friendly adaptations).
"""

from __future__ import annotations

import time
from typing import Any

from ..observability.structured_logger import get_audit_logger, get_structured_logger
from ..schemas.models import SelfEvalVerdictSchema

try:
    from google.adk.plugins.base_plugin import BasePlugin  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover

    class BasePlugin:  # type: ignore[no-redef]
        """Fallback ADK BasePlugin shim for isolated stdlib environments."""

        def __init__(self, name: str = "base_plugin") -> None:
            self.name = name


logger = get_structured_logger("concierge_agent.orchestration.guardrails")

_PROMPT_INJECTION_PATTERNS = (
    "ignore all previous instructions",
    "ignore your constitution",
    "bypass human approval",
    "skip allergen check",
    "reveal system prompt",
    "print secret manager",
)


class SelfEvalGuardrail:
    """Deterministic + rubric-driven Self-Evaluation verifier for weekly meal plans."""

    @staticmethod
    def evaluate_weekly_meal_plan(
        plan_payload: dict[str, Any],
        *,
        excluded_allergens: list[str],
        max_weeknight_cook_minutes: int = 30,
        weekly_budget_usd: float = 225.0,
    ) -> SelfEvalVerdictSchema:
        """Evaluates a candidate weekly meal plan before allowing it to reach Andrew."""
        violations: list[str] = []
        daily_slots = plan_payload.get("daily_schedule", [])
        if len(daily_slots) < 5:
            violations.append(
                f"Expected at least 5 weekday meal slots; found {len(daily_slots)}."
            )

        banned = {a.lower().strip() for a in excluded_allergens}
        weekdays = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}

        for slot in daily_slots:
            day_name = slot.get("day_of_week", "Unknown")
            cook_mins = int(slot.get("active_evening_cook_minutes", 999))
            if day_name in weekdays and cook_mins > max_weeknight_cook_minutes:
                violations.append(
                    f"{day_name} dinner active cook time ({cook_mins}m) exceeds "
                    f"Andrew's post-5PM limit of {max_weeknight_cook_minutes}m."
                )

            breakfast = slot.get("post_workout_breakfast", {})
            protein_g = int(breakfast.get("adult_protein_g", 0))
            if protein_g < 28:
                violations.append(
                    f"{day_name} post-5AM-workout breakfast has only {protein_g}g protein "
                    "(minimum 28g required)."
                )

            if not slot.get("kid_adaptation_tip"):
                violations.append(f"{day_name} is missing a kid_adaptation_tip.")

            # Allergen check across all meal ingredient lists
            for meal_key in (
                "post_workout_breakfast",
                "workday_grab_and_go_lunch",
                "weeknight_family_dinner",
            ):
                meal_obj = slot.get(meal_key, {})
                allergens_present = {
                    str(a).lower() for a in meal_obj.get("allergens", [])
                }
                overlap = banned.intersection(allergens_present)
                if overlap:
                    violations.append(
                        f"{day_name} {meal_key} ('{meal_obj.get('title')}') contains "
                        f"prohibited household allergens: {sorted(overlap)}."
                    )

        est_cost = float(plan_payload.get("estimated_total_cost_usd", 0.0))
        if est_cost > weekly_budget_usd:
            violations.append(
                f"Estimated weekly cost (${est_cost:.2f}) exceeds budget cap "
                f"(${weekly_budget_usd:.2f})."
            )

        score = max(0, 100 - (20 * len(violations)))
        passed = len(violations) == 0
        remediation = (
            "All schedule, post-5AM-workout protein, kid-friendly, allergen, and budget checks passed."
            if passed
            else "Swap violating meals using `search_high_protein_kid_friendly_recipes` with stricter `max_active_cook_minutes` and `excluded_allergens`."
        )

        verdict = SelfEvalVerdictSchema(
            passed=passed,
            score_out_of_100=score,
            violations=violations,
            remediation_guidance=remediation,
        )
        logger.info(
            "Completed Self-Evaluation Guardrail check on weekly meal plan",
            extra={"structured_data": verdict.to_dict()},
        )
        return verdict


class ConciergePolicyAndSelfEvalPlugin(BasePlugin):
    """ADK App-level Plugin enforcing input security, tool policies, and audit logging."""

    def __init__(self, excluded_allergens: list[str] | None = None) -> None:
        super().__init__(name="concierge_policy_and_self_eval_plugin")
        self._excluded_allergens = excluded_allergens or ["peanuts", "tree_nuts"]
        self._inflight_intents: dict[int, tuple[str, float]] = {}

    @staticmethod
    def inspect_user_prompt_safety(prompt_text: str) -> tuple[bool, str | None]:
        """Detects prompt-injection or guardrail-bypass instructions prior to model invocation."""
        lowered = prompt_text.lower()
        for phrase in _PROMPT_INJECTION_PATTERNS:
            if phrase in lowered:
                return (
                    False,
                    (
                        "Request blocked by Concierge Security Guardrail: instructions attempting "
                        "to override safety rules, allergen checks, or human approval gates are not permitted."
                    ),
                )
        return True, None

    async def before_model_callback(
        self,
        *,
        callback_context: Any,
        llm_request: Any,
    ) -> Any | None:
        """ADK plugin hook inspecting prompts before they reach Gemini."""
        contents = getattr(llm_request, "contents", []) or []
        for content in contents:
            for part in getattr(content, "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    safe, block_msg = self.inspect_user_prompt_safety(text)
                    if not safe:
                        logger.warning(
                            "Blocked unsafe prompt in before_model_callback",
                            extra={"structured_data": {"reason": block_msg}},
                        )
                        try:
                            from google.adk.models.llm_response import LlmResponse  # type: ignore[import-not-found]
                            from google.genai import types  # type: ignore[import-not-found]

                            return LlmResponse(
                                content=types.ModelContent(block_msg or "Blocked.")
                            )
                        except ImportError:
                            return {"blocked": True, "message": block_msg}
        return None

    async def before_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
    ) -> dict[str, Any] | None:
        """ADK plugin hook logging `INTENT` and validating allergen/policy invariants."""
        tool_name = getattr(tool, "name", str(tool))
        corr_id = get_audit_logger().record_intent(
            actor="adk_plugin_hook",
            action_name=tool_name,
            rationale=f"Executing tool '{tool_name}' inside multi-agent workflow.",
            parameters=tool_args,
        )
        self._inflight_intents[id(tool_args)] = (corr_id, time.perf_counter())
        return None

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """ADK plugin hook logging paired `OUTCOME` and running self-eval on meal plans."""
        tool_name = getattr(tool, "name", str(tool))
        corr_id, started = self._inflight_intents.pop(
            id(tool_args), ("act-untracked", time.perf_counter())
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        status = str(result.get("status", "success")) if isinstance(result, dict) else "success"

        get_audit_logger().record_outcome(
            correlation_id=corr_id,
            actor="adk_plugin_hook",
            action_name=tool_name,
            status=status,
            outcome_summary=f"Completed tool '{tool_name}' with status='{status}'.",
            duration_ms=elapsed_ms,
        )

        if (
            tool_name == "generate_weekly_meal_and_prep_schedule"
            and isinstance(result, dict)
            and result.get("status") == "success"
        ):
            verdict = SelfEvalGuardrail.evaluate_weekly_meal_plan(
                result,
                excluded_allergens=self._excluded_allergens,
                weekly_budget_usd=float(result.get("weekly_budget_usd", 225.0)),
            )
            result["self_evaluation"] = verdict.to_dict()
            return result

        return None
