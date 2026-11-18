"""Google ADK Multi-Agent Architecture for Andrew's Family Meal & Wellness Command Center.

Defines:
  - `root_agent`: Coordinator `LlmAgent` enforcing the Intake-First Check-In protocol and
    delegating to specialized ADK orchestration pipelines.
  - `weekly_meal_orchestration_pipeline`: `SequentialAgent` chaining:
      1. `parallel_research_and_inventory_stage` (`ParallelAgent` running recipe research
         and schedule/pantry audit concurrently on `gemini-2.5-flash`).
      2. `meal_plan_composer_and_critic_loop` (`LoopAgent` pairing `weekly_meal_architect_agent`
         on `gemini-2.5-pro` with `schedule_and_nutrition_critic_agent` on `gemini-2.5-flash`).
      3. `aisle_grocery_consolidator_agent` (`LlmAgent` staging pantry-deducted carts with HITL).
      4. `rapid_cooking_timeline_agent` (`LlmAgent` producing <=25-min Cook Mode timelines).
  - `app`: ADK `App` configured with `EventsCompactionConfig`, `ContextCacheConfig`,
    `ResumabilityConfig(is_resumable=True)`, and `ConciergePolicyAndSelfEvalPlugin`.
  - `ConciergeCommandCenterOrchestrator`: Deterministic local + ADK execution facade used by
    the Command Center UI and automated evaluation suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config.settings import get_settings
from .memory.compaction import (
    ContextCompactionManager,
    build_adk_context_cache_config,
    build_adk_events_compaction_config,
)
from .memory.persistent_store import get_memory_store
from .observability.tracing import get_tracer
from .orchestration.guardrails_plugin import ConciergePolicyAndSelfEvalPlugin
from .orchestration.hitl_hooks import get_approval_gate
from .orchestration.model_router import get_model_router
from .prompts.constitution import (
    AISLE_GROCERY_CONSOLIDATOR_INSTRUCTION,
    COOKING_TIMELINE_COACH_INSTRUCTION,
    COORDINATOR_SYSTEM_CONSTITUTION,
    INTAKE_READINESS_INSTRUCTION,
    MEAL_ARCHITECT_INSTRUCTION,
    NUTRITION_RECIPE_RESEARCH_INSTRUCTION,
    SCHEDULE_AND_NUTRITION_CRITIC_INSTRUCTION,
    SCHEDULE_PANTRY_AUDIT_INSTRUCTION,
)
from .tools import (
    collect_weekly_blocker_inputs_for_andrew,
    consolidate_aisle_sorted_grocery_list,
    fetch_family_dietary_profile_and_schedule,
    generate_step_by_step_cooking_timeline,
    generate_weekly_meal_and_prep_schedule,
    persist_weekly_family_meal_feedback,
    publish_prep_blocks_to_family_calendar_with_approval,
    search_high_protein_kid_friendly_recipes,
    submit_grocery_cart_order_with_approval,
    validate_recipe_against_family_allergens_and_time,
)

try:
    from google.adk.agents import (  # type: ignore[import-not-found]
        LlmAgent,
        LoopAgent,
        ParallelAgent,
        SequentialAgent,
    )
    from google.adk.apps import App, ResumabilityConfig  # type: ignore[import-not-found]
    from google.adk.tools import FunctionTool, exit_loop  # type: ignore[import-not-found]

    HAS_GOOGLE_ADK = True
except ImportError:  # pragma: no cover - lightweight ADK topology descriptors for bare stdlib

    HAS_GOOGLE_ADK = False

    @dataclass
    class FunctionTool:  # type: ignore[no-redef]
        func: Any
        require_confirmation: bool = False

        @property
        def name(self) -> str:
            return getattr(self.func, "__name__", "tool")

    def exit_loop() -> dict[str, str]:
        """Signals LoopAgent termination once self-evaluation passes."""
        return {"status": "loop_complete"}

    @dataclass
    class LlmAgent:  # type: ignore[no-redef]
        name: str
        model: str = "gemini-2.5-flash"
        description: str = ""
        instruction: str = ""
        tools: list[Any] = field(default_factory=list)
        sub_agents: list[Any] = field(default_factory=list)
        output_key: str | None = None

    @dataclass
    class SequentialAgent:  # type: ignore[no-redef]
        name: str
        description: str = ""
        sub_agents: list[Any] = field(default_factory=list)

    @dataclass
    class ParallelAgent:  # type: ignore[no-redef]
        name: str
        description: str = ""
        sub_agents: list[Any] = field(default_factory=list)

    @dataclass
    class LoopAgent:  # type: ignore[no-redef]
        name: str
        description: str = ""
        sub_agents: list[Any] = field(default_factory=list)
        max_iterations: int = 2

    @dataclass
    class ResumabilityConfig:  # type: ignore[no-redef]
        is_resumable: bool = True

    @dataclass
    class App:  # type: ignore[no-redef]
        name: str
        root_agent: Any
        plugins: list[Any] = field(default_factory=list)
        events_compaction_config: Any = None
        context_cache_config: Any = None
        resumability_config: Any = None


_settings = get_settings()

# High-stakes tools wrapped with ADK native `FunctionTool(..., require_confirmation=True)`
hitl_grocery_order_tool = FunctionTool(
    submit_grocery_cart_order_with_approval,
    require_confirmation=True,
)
hitl_calendar_publish_tool = FunctionTool(
    publish_prep_blocks_to_family_calendar_with_approval,
    require_confirmation=True,
)

# 1. Intake & Blocker Gate Agent (Flash tier — fast pre-planning check)
intake_readiness_agent = LlmAgent(
    name="intake_readiness_agent",
    model=_settings.fast_model_name,
    description=(
        "Surfaces any missing weekly inputs from Andrew (schedule exceptions, Sunday prep "
        "minutes, budget, cravings) before orchestrating the weekly meal plan."
    ),
    instruction=INTAKE_READINESS_INSTRUCTION,
    tools=[
        fetch_family_dietary_profile_and_schedule,
        collect_weekly_blocker_inputs_for_andrew,
    ],
    output_key="intake_check_result",
)

# 2A. Parallel Branch 1: Nutrition & Kid-Friendly Recipe Research (Flash tier)
nutrition_and_recipe_research_agent = LlmAgent(
    name="nutrition_and_recipe_research_agent",
    model=_settings.fast_model_name,
    description=(
        "Searches high-protein post-5AM-workout breakfasts, grab-and-go workday lunches, "
        "and <=25-minute nut-free family dinners."
    ),
    instruction=NUTRITION_RECIPE_RESEARCH_INSTRUCTION,
    tools=[
        search_high_protein_kid_friendly_recipes,
        validate_recipe_against_family_allergens_and_time,
    ],
    output_key="curated_recipe_candidates",
)

# 2B. Parallel Branch 2: Household Schedule & Pantry Inventory Audit (Flash tier)
schedule_and_pantry_audit_agent = LlmAgent(
    name="schedule_and_pantry_audit_agent",
    model=_settings.fast_model_name,
    description=(
        "Audits Andrew's 5AM workout, 8AM-5PM work schedule, evening kid routines, and "
        "pantry staples on hand."
    ),
    instruction=SCHEDULE_PANTRY_AUDIT_INSTRUCTION,
    tools=[fetch_family_dietary_profile_and_schedule],
    output_key="weekly_schedule_and_pantry_context",
)

parallel_research_and_inventory_stage = ParallelAgent(
    name="parallel_research_and_inventory_stage",
    description="Concurrently researches compliant family recipes and audits schedule/pantry inventory.",
    sub_agents=[
        nutrition_and_recipe_research_agent,
        schedule_and_pantry_audit_agent,
    ],
)

# 3. Pro-Tier Weekly Meal & Batch-Prep Architect + Self-Eval Critic Loop
weekly_meal_architect_agent = LlmAgent(
    name="weekly_meal_architect_agent",
    model=_settings.planning_model_name,
    description=(
        "Synthesizes the 7-day breakfast/lunch/dinner schedule and 75-minute Sunday "
        "batch-prep gameplan using Pro-tier multi-constraint reasoning."
    ),
    instruction=MEAL_ARCHITECT_INSTRUCTION,
    tools=[generate_weekly_meal_and_prep_schedule],
    output_key="draft_weekly_meal_plan",
)

schedule_and_nutrition_critic_agent = LlmAgent(
    name="schedule_and_nutrition_critic_agent",
    model=_settings.fast_model_name,
    description=(
        "Evaluates draft meal plans against Andrew's 30-min weeknight cook limit, "
        "post-workout protein floor, allergen bans, and budget ceiling."
    ),
    instruction=SCHEDULE_AND_NUTRITION_CRITIC_INSTRUCTION,
    tools=[validate_recipe_against_family_allergens_and_time, exit_loop],
    output_key="meal_plan_self_eval_verdict",
)

meal_plan_composer_and_critic_loop = LoopAgent(
    name="meal_plan_composer_and_critic_loop",
    description="Iteratively composes and self-evaluates the 7-day meal plan until 100% compliant.",
    sub_agents=[
        weekly_meal_architect_agent,
        schedule_and_nutrition_critic_agent,
    ],
    max_iterations=2,
)

# 4. Smart Aisle Grocery Consolidator Agent (Flash tier + HITL cart staging)
aisle_grocery_consolidator_agent = LlmAgent(
    name="aisle_grocery_consolidator_agent",
    model=_settings.fast_model_name,
    description=(
        "Deduplicates 7 days of ingredients, subtracts pantry staples, sorts by store aisle, "
        "and stages the cart behind a Human-in-the-Loop approval gate."
    ),
    instruction=AISLE_GROCERY_CONSOLIDATOR_INSTRUCTION,
    tools=[
        consolidate_aisle_sorted_grocery_list,
        hitl_grocery_order_tool,
    ],
    output_key="staged_grocery_cart",
)

# 5. Rapid Cooking Timeline Coach Agent (Flash tier + async feedback memory)
rapid_cooking_timeline_agent = LlmAgent(
    name="rapid_cooking_timeline_agent",
    model=_settings.fast_model_name,
    description=(
        "Produces parallelized <=25-minute weeknight cooking timelines, kid-portion split "
        "checkpoints, and async post-meal preference memory updates."
    ),
    instruction=COOKING_TIMELINE_COACH_INSTRUCTION,
    tools=[
        generate_step_by_step_cooking_timeline,
        hitl_calendar_publish_tool,
        persist_weekly_family_meal_feedback,
    ],
    output_key="evening_cooking_timelines",
)

# End-to-End Sequential Orchestration Pipeline
weekly_meal_orchestration_pipeline = SequentialAgent(
    name="weekly_meal_orchestration_pipeline",
    description=(
        "Executes parallel recipe/pantry discovery, Pro meal-plan synthesis + self-eval loop, "
        "aisle-sorted grocery consolidation, and rapid Cook-Mode timeline generation."
    ),
    sub_agents=[
        parallel_research_and_inventory_stage,
        meal_plan_composer_and_critic_loop,
        aisle_grocery_consolidator_agent,
        rapid_cooking_timeline_agent,
    ],
)

# Root Coordinator Agent discovered by `adk web` / `adk run`
root_agent = LlmAgent(
    name="andrew_concierge_command_center",
    model=_settings.fast_model_name,
    description=(
        "Andrew's Personal Family Meal & Wellness Command Center Coordinator. Surfaces "
        "needed weekly inputs first, then orchestrates research, planning, shopping, and cooking."
    ),
    instruction=COORDINATOR_SYSTEM_CONSTITUTION,
    tools=[
        fetch_family_dietary_profile_and_schedule,
        collect_weekly_blocker_inputs_for_andrew,
        generate_step_by_step_cooking_timeline,
        hitl_grocery_order_tool,
        hitl_calendar_publish_tool,
        persist_weekly_family_meal_feedback,
    ],
    sub_agents=[
        intake_readiness_agent,
        weekly_meal_orchestration_pipeline,
    ],
)

# ADK Application wrapping root_agent with Compaction, Context Caching, Resumability, and Guardrail Plugin
app = App(
    name="andrew_family_concierge_app",
    root_agent=root_agent,
    plugins=[ConciergePolicyAndSelfEvalPlugin()],
    events_compaction_config=build_adk_events_compaction_config(
        compaction_interval=4,
        overlap_size=1,
        token_threshold=12000,
        event_retention_size=6,
    ),
    context_cache_config=build_adk_context_cache_config(
        cache_intervals=15,
        ttl_seconds=1800,
        min_tokens=2048,
    ),
    resumability_config=ResumabilityConfig(is_resumable=True),
)


class ConciergeCommandCenterOrchestrator:
    """High-level orchestration facade driving the multi-agent pipeline for UI & evals."""

    def __init__(self) -> None:
        self.compactor = ContextCompactionManager()
        self.plugin = ConciergePolicyAndSelfEvalPlugin()
        self.router = get_model_router()
        self.store = get_memory_store()

    def handle_command_center_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        checkin_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Executes a traced, guardrail-protected Command Center turn end-to-end."""
        with get_tracer().start_span(
            "concierge_coordinator.turn",
            attributes={"session_id": session_id},
        ) as root_span:
            # 1. Input Prompt-Injection & Policy Guardrail
            is_safe, block_reason = self.plugin.inspect_user_prompt_safety(user_message)
            if not is_safe:
                root_span.set_status("BLOCKED", block_reason)
                return {
                    "status": "BLOCKED_BY_GUARDRAIL",
                    "message": block_reason,
                    "trace_id": root_span.trace_id,
                }

            # 2. Persist & Compact Session History
            self.store.append_turn(session_id, "user", user_message)
            history = self.store.get_session_turns(session_id, limit=40)
            _, compaction_stats = self.compactor.compact_conversation_history(history)

            # 3. Route & Check Intake Readiness BEFORE Orchestrating
            self.router.select_model(task_type="intake_check", user_query=user_message)
            profile_res = fetch_family_dietary_profile_and_schedule()
            profile = profile_res["family_profile"]

            inputs = dict(checkin_inputs or {})
            week_start = str(inputs.get("week_start_date", "2026-11-23"))
            use_defaults = bool(
                inputs.get("use_standard_defaults", False)
                or "default" in user_message.lower()
                or "orchestrate" in user_message.lower()
            )

            intake_res = collect_weekly_blocker_inputs_for_andrew(
                week_start_date=week_start,
                late_work_or_travel_days=inputs.get("late_work_or_travel_days"),
                sunday_prep_minutes_available=inputs.get("sunday_prep_minutes_available"),
                weekly_budget_usd=inputs.get("weekly_budget_usd"),
                workout_focus=inputs.get("workout_focus"),
                use_up_fridge_items=inputs.get("use_up_fridge_items"),
                kid_or_spouse_cravings=inputs.get("kid_or_spouse_cravings"),
                use_standard_defaults=use_defaults,
            )

            if intake_res.get("status") == "error":
                return {
                    "status": "ERROR",
                    "error": intake_res,
                    "trace_id": root_span.trace_id,
                }

            if not intake_res["ready_to_orchestrate"]:
                assistant_reply = intake_res["prompt_for_andrew"]
                self.store.append_turn(session_id, "assistant", assistant_reply)
                return {
                    "status": "NEEDS_ANDREW_INPUT",
                    "intake_check": intake_res,
                    "family_profile": profile,
                    "compaction_telemetry": compaction_stats,
                    "trace_id": root_span.trace_id,
                }

            # 4. Run Sequential + Parallel Multi-Agent Orchestration Pipeline
            ctx = intake_res["normalized_weekly_context"]
            excluded_allergens = inputs.get(
                "excluded_allergens", profile.get("excluded_allergens", ["peanuts", "tree_nuts"])
            )

            with get_tracer().start_span("pipeline.parallel_research_and_inventory"):
                candidates = search_high_protein_kid_friendly_recipes(
                    meal_category="all",
                    max_active_cook_minutes=30,
                    min_protein_grams_per_adult_serving=28,
                    excluded_allergens=excluded_allergens,
                    prefer_ingredients=ctx["use_up_fridge_items"],
                )

            with get_tracer().start_span("pipeline.meal_plan_composer_and_critic_loop"):
                meal_plan = generate_weekly_meal_and_prep_schedule(
                    week_start_date=ctx["week_start_date"],
                    weekly_budget_usd=float(ctx["weekly_budget_usd"]),
                    sunday_prep_minutes=int(ctx["sunday_prep_minutes_available"]),
                    late_work_or_travel_days=ctx["late_work_or_travel_days"],
                    workout_focus=ctx["workout_focus"],
                    excluded_allergens=excluded_allergens,
                    use_up_fridge_items=ctx["use_up_fridge_items"],
                    kid_or_spouse_cravings=ctx["kid_or_spouse_cravings"],
                )

            with get_tracer().start_span("pipeline.aisle_grocery_consolidator"):
                grocery_cart = consolidate_aisle_sorted_grocery_list(
                    plan_id=meal_plan["plan_id"],
                    recipe_ids=meal_plan["selected_recipe_ids"],
                    subtract_pantry_inventory=True,
                    weekly_budget_usd=float(ctx["weekly_budget_usd"]),
                )

            with get_tracer().start_span("pipeline.rapid_cooking_timeline"):
                monday_dinner_id = meal_plan["daily_schedule"][0]["weeknight_family_dinner"][
                    "recipe_id"
                ]
                tonight_timeline = generate_step_by_step_cooking_timeline(
                    recipe_id=monday_dinner_id,
                    target_serve_time_cst="17:45 CST",
                    sunday_prep_completed=int(ctx["sunday_prep_minutes_available"]) > 0,
                )

            summary_msg = (
                f"Orchestrated {meal_plan['plan_id']} for week of {ctx['week_start_date']}: "
                f"7 days scheduled around your 5:00-6:00 AM CST workouts and 8:00 AM-5:00 PM "
                f"workday, {grocery_cart['total_items']} grocery items consolidated "
                f"(${grocery_cart['estimated_subtotal_usd']:.2f}, awaiting your approval)."
            )
            self.store.append_turn(session_id, "assistant", summary_msg)

            return {
                "status": "ORCHESTRATED",
                "summary": summary_msg,
                "intake_context": ctx,
                "recipe_candidates_count": candidates.get("candidate_count", 0),
                "weekly_meal_plan": meal_plan,
                "staged_grocery_cart": grocery_cart,
                "featured_cooking_timeline": tonight_timeline,
                "pending_hitl_approvals": get_approval_gate().list_pending_approvals(),
                "compaction_telemetry": compaction_stats,
                "trace_id": root_span.trace_id,
            }
