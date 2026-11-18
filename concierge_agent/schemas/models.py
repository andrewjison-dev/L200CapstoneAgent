"""Explicit Pydantic v2 JSON Schemas constraining tool inputs, outputs, and LLM handoffs.

Uses `ConfigDict(extra="forbid")` when Pydantic is installed and provides an equivalent
strict JSON-schema validator fallback for zero-dependency isolated environments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError  # type: ignore[import-not-found]

    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover - fallback for isolated stdlib execution
    HAS_PYDANTIC = False
    BaseModel = object  # type: ignore[assignment,misc]
    ConfigDict = dict  # type: ignore[assignment,misc]
    ValidationError = ValueError  # type: ignore[assignment,misc]

    def Field(default: Any = ..., **kwargs: Any) -> Any:  # noqa: N802
        return default if default is not ... else None


class SchemaValidationError(ValueError):
    """Raised when a tool input or output violates its strict JSON schema."""


T = TypeVar("T")


if HAS_PYDANTIC:

    class _StrictModel(BaseModel):
        model_config = ConfigDict(extra="forbid", validate_assignment=True)

        def to_dict(self) -> dict[str, Any]:
            return self.model_dump()

        @classmethod
        def json_schema(cls) -> dict[str, Any]:
            return cls.model_json_schema()

    class ToolErrorEnvelopeSchema(_StrictModel):
        """Standardized guided-recovery error payload returned to the LLM on failure."""

        status: str = Field(default="error", pattern="^error$")
        error_code: str = Field(..., min_length=3, description="Machine-readable error code.")
        message: str = Field(..., min_length=5, description="Human-readable explanation of what failed.")
        recovery_instructions: str = Field(
            ...,
            min_length=10,
            description="Actionable step-by-step instructions telling the LLM how to recover.",
        )
        invalid_fields: list[str] = Field(default_factory=list)

    class WeeklyBlockerCheckInputSchema(_StrictModel):
        """Input schema for checking missing weekly inputs from Andrew prior to planning."""

        week_start_date: str = Field(
            ...,
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="ISO-8601 Monday start date (YYYY-MM-DD) for the target week.",
        )
        late_work_or_travel_days: list[str] = Field(
            default_factory=list,
            description="Weekdays where Andrew or his spouse have post-5PM commitments or travel.",
        )
        sunday_prep_minutes_available: int | None = Field(
            default=None,
            ge=0,
            le=180,
            description="Minutes available on Sunday for batch meal prep (0-180).",
        )
        weekly_budget_usd: float | None = Field(
            default=None,
            ge=50.0,
            le=1000.0,
            description="Target weekly grocery budget cap in USD.",
        )
        workout_focus: str | None = Field(
            default=None,
            description="5AM-6AM CST morning workout focus (e.g., 'strength_hypertrophy', 'endurance', 'balanced').",
        )
        use_up_fridge_items: list[str] = Field(
            default_factory=list,
            description="Existing perishable fridge/pantry ingredients to prioritize first.",
        )
        kid_or_spouse_cravings: list[str] = Field(
            default_factory=list,
            description="Specific meal requests or picky-eater notes from spouse or kids.",
        )
        use_standard_defaults: bool = Field(
            default=False,
            description="Set True when Andrew explicitly approves using his saved household defaults.",
        )

    class WeeklyBlockerCheckOutputSchema(_StrictModel):
        """Output schema indicating whether all pre-planning inputs from Andrew are satisfied."""

        status: str = Field(default="success")
        ready_to_orchestrate: bool
        missing_inputs: list[str]
        prompt_for_andrew: str
        normalized_weekly_context: dict[str, Any]

    class RecipeSearchInputSchema(_StrictModel):
        """Strict input schema for querying high-protein, kid-friendly family recipes."""

        meal_category: str = Field(
            ...,
            pattern=r"^(breakfast|lunch|dinner|all)$",
            description="Meal slot filter: 'breakfast', 'lunch', 'dinner', or 'all'.",
        )
        max_active_cook_minutes: int = Field(
            default=25,
            ge=5,
            le=60,
            description="Maximum active stove/prep minutes allowed on a busy weekday.",
        )
        min_protein_grams_per_adult_serving: int = Field(
            default=28,
            ge=10,
            le=80,
            description="Minimum protein grams per adult serving to support 5AM workout recovery.",
        )
        excluded_allergens: list[str] = Field(
            default_factory=list,
            description="Allergens strictly banned from returned recipes (e.g., 'peanuts', 'tree_nuts').",
        )
        prefer_ingredients: list[str] = Field(
            default_factory=list,
            description="Pantry or fridge ingredients to prioritize.",
        )

    class RecipeSearchOutputSchema(_StrictModel):
        """Output schema containing validated candidate recipes."""

        status: str = Field(default="success")
        candidate_count: int = Field(..., ge=0)
        recipes: list[dict[str, Any]]

    class DailyMealSlotSchema(_StrictModel):
        """Schema for a single day's schedule-aligned meals for Andrew's family of 4."""

        day_of_week: str
        morning_workout_window_cst: str = Field(default="05:00-06:00 CST")
        post_workout_breakfast: dict[str, Any]
        workday_grab_and_go_lunch: dict[str, Any]
        weeknight_family_dinner: dict[str, Any]
        kid_adaptation_tip: str
        active_evening_cook_minutes: int = Field(..., ge=0, le=30)

    class WeeklyMealPlanInputSchema(_StrictModel):
        """Strict input schema for generating Andrew's 7-day meal & Sunday prep plan."""

        week_start_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
        weekly_budget_usd: float = Field(default=225.0, ge=50.0, le=1000.0)
        sunday_prep_minutes: int = Field(default=75, ge=0, le=180)
        late_work_or_travel_days: list[str] = Field(default_factory=list)
        workout_focus: str = Field(default="balanced_strength_and_wellness")
        excluded_allergens: list[str] = Field(default_factory=list)
        use_up_fridge_items: list[str] = Field(default_factory=list)
        kid_or_spouse_cravings: list[str] = Field(default_factory=list)

    class WeeklyMealPlanOutputSchema(_StrictModel):
        """Structured 7-day meal schedule + Sunday batch-prep gameplan."""

        status: str = Field(default="success")
        plan_id: str
        week_start_date: str
        sunday_batch_prep_gameplan: list[dict[str, Any]]
        daily_schedule: list[DailyMealSlotSchema]
        estimated_total_cost_usd: float
        weekly_budget_usd: float
        macro_summary_adult_daily_avg: dict[str, int]

    class GroceryCartItemSchema(_StrictModel):
        """Single consolidated grocery line item sorted by store aisle."""

        ingredient_name: str
        aisle_category: str
        total_quantity: float = Field(..., gt=0)
        unit: str
        pantry_deducted_quantity: float = Field(default=0.0, ge=0)
        estimated_cost_usd: float = Field(..., ge=0)
        used_in_meals: list[str]

    class GroceryConsolidationInputSchema(_StrictModel):
        """Strict input schema for building an aisle-sorted, pantry-deducted grocery cart."""

        plan_id: str = Field(..., min_length=4)
        recipe_ids: list[str] = Field(..., min_length=1)
        household_size_adults: int = Field(default=2, ge=1, le=6)
        household_size_kids: int = Field(default=2, ge=0, le=6)
        subtract_pantry_inventory: bool = Field(default=True)
        weekly_budget_usd: float = Field(default=225.0, ge=50.0, le=1000.0)

    class GroceryConsolidationOutputSchema(_StrictModel):
        """Output schema for staged grocery cart awaiting human approval."""

        status: str = Field(default="success")
        cart_id: str
        approval_status: str = Field(default="PENDING_HUMAN_APPROVAL")
        total_items: int
        pantry_items_skipped: list[str]
        estimated_subtotal_usd: float
        within_budget: bool
        aisle_grouped_items: dict[str, list[GroceryCartItemSchema]]

    class GroceryOrderSubmissionInputSchema(_StrictModel):
        """Input schema for high-stakes grocery checkout requiring explicit HITL approval."""

        cart_id: str = Field(..., min_length=4)
        delivery_or_pickup_slot: str = Field(..., min_length=5)
        human_approval_confirmed: bool = Field(
            default=False,
            description="MUST be True via Andrew's explicit approval token before order dispatch.",
        )
        approval_token: str | None = Field(default=None)

    class GroceryOrderSubmissionOutputSchema(_StrictModel):
        """Output schema for grocery cart submission or HITL pause."""

        status: str
        cart_id: str
        order_reference: str | None = None
        requires_human_confirmation: bool
        confirmation_prompt: str | None = None

    class CookingTimelineRequestSchema(_StrictModel):
        """Input schema for generating a parallelized 25-minute cooking execution timeline."""

        recipe_id: str = Field(..., min_length=3)
        target_serve_time_cst: str = Field(
            default="17:45 CST",
            description="Target family table time in America/Chicago.",
        )
        sunday_prep_completed: bool = Field(default=True)

    class CookingStepSchema(_StrictModel):
        """Single timed step in Cook Mode."""

        minute_offset: int = Field(..., ge=0, le=45)
        station: str
        instruction: str
        kid_friendly_checkpoint: str | None = None

    class CookingTimelineOutputSchema(_StrictModel):
        """Output schema for step-by-step Cook Mode execution."""

        status: str = Field(default="success")
        recipe_id: str
        recipe_title: str
        target_serve_time_cst: str
        total_active_minutes: int
        steps: list[CookingStepSchema]

    class MealFeedbackInputSchema(_StrictModel):
        """Input schema for persisting async weekly family meal ratings & pantry updates."""

        recipe_id: str = Field(..., min_length=3)
        andrew_rating: int = Field(..., ge=1, le=5)
        spouse_rating: int = Field(..., ge=1, le=5)
        kids_ate_cleanly: bool
        notes: str = Field(default="", max_length=500)

    class SelfEvalVerdictSchema(_StrictModel):
        """Output schema for automated self-evaluation guardrail verification."""

        passed: bool
        score_out_of_100: int = Field(..., ge=0, le=100)
        violations: list[str]
        remediation_guidance: str

else:
    # Lightweight strict validator dataclasses when running in zero-package stdlib environment
    @dataclass
    class _FallbackBase:
        def to_dict(self) -> dict[str, Any]:
            return asdict(self)

        @classmethod
        def json_schema(cls) -> dict[str, Any]:
            hints = get_type_hints(cls)
            props = {name: {"type": str(tp)} for name, tp in hints.items()}
            return {
                "title": cls.__name__,
                "type": "object",
                "additionalProperties": False,
                "properties": props,
                "required": [
                    f.name
                    for f in fields(cls)
                    if f.default is f.default_factory is None  # type: ignore[comparison-overlap]
                ],
            }

    @dataclass
    class ToolErrorEnvelopeSchema(_FallbackBase):
        error_code: str
        message: str
        recovery_instructions: str
        status: str = "error"
        invalid_fields: list[str] = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            if self.invalid_fields is None:
                self.invalid_fields = []

    @dataclass
    class WeeklyBlockerCheckInputSchema(_FallbackBase):
        week_start_date: str
        late_work_or_travel_days: list[str] = None  # type: ignore[assignment]
        sunday_prep_minutes_available: int | None = None
        weekly_budget_usd: float | None = None
        workout_focus: str | None = None
        use_up_fridge_items: list[str] = None  # type: ignore[assignment]
        kid_or_spouse_cravings: list[str] = None  # type: ignore[assignment]
        use_standard_defaults: bool = False

        def __post_init__(self) -> None:
            import re

            if not isinstance(self.week_start_date, str) or not re.match(
                r"^\d{4}-\d{2}-\d{2}$", self.week_start_date
            ):
                raise SchemaValidationError(
                    "week_start_date must match YYYY-MM-DD ISO format."
                )
            if self.sunday_prep_minutes_available is not None and not (
                0 <= int(self.sunday_prep_minutes_available) <= 180
            ):
                raise SchemaValidationError(
                    "sunday_prep_minutes_available must be between 0 and 180."
                )
            if self.weekly_budget_usd is not None and not (
                50.0 <= float(self.weekly_budget_usd) <= 1000.0
            ):
                raise SchemaValidationError(
                    "weekly_budget_usd must be between 50.0 and 1000.0."
                )
            self.late_work_or_travel_days = list(self.late_work_or_travel_days or [])
            self.use_up_fridge_items = list(self.use_up_fridge_items or [])
            self.kid_or_spouse_cravings = list(self.kid_or_spouse_cravings or [])

    @dataclass
    class WeeklyBlockerCheckOutputSchema(_FallbackBase):
        ready_to_orchestrate: bool
        missing_inputs: list[str]
        prompt_for_andrew: str
        normalized_weekly_context: dict[str, Any]
        status: str = "success"

    @dataclass
    class RecipeSearchInputSchema(_FallbackBase):
        meal_category: str
        max_active_cook_minutes: int = 25
        min_protein_grams_per_adult_serving: int = 28
        excluded_allergens: list[str] = None  # type: ignore[assignment]
        prefer_ingredients: list[str] = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            if self.meal_category not in {"breakfast", "lunch", "dinner", "all"}:
                raise SchemaValidationError(
                    "meal_category must be one of: breakfast, lunch, dinner, all."
                )
            if not (5 <= int(self.max_active_cook_minutes) <= 60):
                raise SchemaValidationError(
                    "max_active_cook_minutes must be between 5 and 60."
                )
            if not (10 <= int(self.min_protein_grams_per_adult_serving) <= 80):
                raise SchemaValidationError(
                    "min_protein_grams_per_adult_serving must be between 10 and 80."
                )
            self.excluded_allergens = list(self.excluded_allergens or [])
            self.prefer_ingredients = list(self.prefer_ingredients or [])

    @dataclass
    class RecipeSearchOutputSchema(_FallbackBase):
        candidate_count: int
        recipes: list[dict[str, Any]]
        status: str = "success"

    @dataclass
    class DailyMealSlotSchema(_FallbackBase):
        day_of_week: str
        post_workout_breakfast: dict[str, Any]
        workday_grab_and_go_lunch: dict[str, Any]
        weeknight_family_dinner: dict[str, Any]
        kid_adaptation_tip: str
        active_evening_cook_minutes: int
        morning_workout_window_cst: str = "05:00-06:00 CST"

    @dataclass
    class WeeklyMealPlanInputSchema(_FallbackBase):
        week_start_date: str
        weekly_budget_usd: float = 225.0
        sunday_prep_minutes: int = 75
        late_work_or_travel_days: list[str] = None  # type: ignore[assignment]
        workout_focus: str = "balanced_strength_and_wellness"
        excluded_allergens: list[str] = None  # type: ignore[assignment]
        use_up_fridge_items: list[str] = None  # type: ignore[assignment]
        kid_or_spouse_cravings: list[str] = None  # type: ignore[assignment]

        def __post_init__(self) -> None:
            import re

            if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(self.week_start_date)):
                raise SchemaValidationError("week_start_date must match YYYY-MM-DD.")
            if not (50.0 <= float(self.weekly_budget_usd) <= 1000.0):
                raise SchemaValidationError("weekly_budget_usd must be 50..1000.")
            self.late_work_or_travel_days = list(self.late_work_or_travel_days or [])
            self.excluded_allergens = list(self.excluded_allergens or [])
            self.use_up_fridge_items = list(self.use_up_fridge_items or [])
            self.kid_or_spouse_cravings = list(self.kid_or_spouse_cravings or [])

    @dataclass
    class WeeklyMealPlanOutputSchema(_FallbackBase):
        plan_id: str
        week_start_date: str
        sunday_batch_prep_gameplan: list[dict[str, Any]]
        daily_schedule: list[DailyMealSlotSchema]
        estimated_total_cost_usd: float
        weekly_budget_usd: float
        macro_summary_adult_daily_avg: dict[str, int]
        status: str = "success"

    @dataclass
    class GroceryCartItemSchema(_FallbackBase):
        ingredient_name: str
        aisle_category: str
        total_quantity: float
        unit: str
        estimated_cost_usd: float
        used_in_meals: list[str]
        pantry_deducted_quantity: float = 0.0

    @dataclass
    class GroceryConsolidationInputSchema(_FallbackBase):
        plan_id: str
        recipe_ids: list[str]
        household_size_adults: int = 2
        household_size_kids: int = 2
        subtract_pantry_inventory: bool = True
        weekly_budget_usd: float = 225.0

        def __post_init__(self) -> None:
            if not self.plan_id or len(self.plan_id) < 4:
                raise SchemaValidationError("plan_id must be at least 4 characters.")
            if not self.recipe_ids:
                raise SchemaValidationError("recipe_ids must contain at least 1 recipe.")

    @dataclass
    class GroceryConsolidationOutputSchema(_FallbackBase):
        cart_id: str
        total_items: int
        pantry_items_skipped: list[str]
        estimated_subtotal_usd: float
        within_budget: bool
        aisle_grouped_items: dict[str, list[GroceryCartItemSchema]]
        approval_status: str = "PENDING_HUMAN_APPROVAL"
        status: str = "success"

    @dataclass
    class GroceryOrderSubmissionInputSchema(_FallbackBase):
        cart_id: str
        delivery_or_pickup_slot: str
        human_approval_confirmed: bool = False
        approval_token: str | None = None

        def __post_init__(self) -> None:
            if not self.cart_id or len(self.cart_id) < 4:
                raise SchemaValidationError("cart_id must be at least 4 characters.")
            if not self.delivery_or_pickup_slot or len(self.delivery_or_pickup_slot) < 5:
                raise SchemaValidationError(
                    "delivery_or_pickup_slot must be at least 5 characters."
                )

    @dataclass
    class GroceryOrderSubmissionOutputSchema(_FallbackBase):
        status: str
        cart_id: str
        requires_human_confirmation: bool
        order_reference: str | None = None
        confirmation_prompt: str | None = None

    @dataclass
    class CookingTimelineRequestSchema(_FallbackBase):
        recipe_id: str
        target_serve_time_cst: str = "17:45 CST"
        sunday_prep_completed: bool = True

        def __post_init__(self) -> None:
            if not self.recipe_id or len(self.recipe_id) < 3:
                raise SchemaValidationError("recipe_id must be at least 3 characters.")

    @dataclass
    class CookingStepSchema(_FallbackBase):
        minute_offset: int
        station: str
        instruction: str
        kid_friendly_checkpoint: str | None = None

    @dataclass
    class CookingTimelineOutputSchema(_FallbackBase):
        recipe_id: str
        recipe_title: str
        target_serve_time_cst: str
        total_active_minutes: int
        steps: list[CookingStepSchema]
        status: str = "success"

    @dataclass
    class MealFeedbackInputSchema(_FallbackBase):
        recipe_id: str
        andrew_rating: int
        spouse_rating: int
        kids_ate_cleanly: bool
        notes: str = ""

        def __post_init__(self) -> None:
            if not (1 <= int(self.andrew_rating) <= 5) or not (
                1 <= int(self.spouse_rating) <= 5
            ):
                raise SchemaValidationError(
                    "andrew_rating and spouse_rating must be integers between 1 and 5."
                )

    @dataclass
    class SelfEvalVerdictSchema(_FallbackBase):
        passed: bool
        score_out_of_100: int
        violations: list[str]
        remediation_guidance: str


def validate_payload(schema_cls: type[T], payload: dict[str, Any]) -> T:
    """Validates a dictionary strictly against `schema_cls` (forbidding unknown fields)."""
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"Expected JSON object for {schema_cls.__name__}.")

    if HAS_PYDANTIC:
        try:
            return schema_cls.model_validate(payload)  # type: ignore[attr-defined,no-any-return]
        except ValidationError as exc:
            raise SchemaValidationError(str(exc)) from exc

    if is_dataclass(schema_cls):
        allowed = {f.name for f in fields(schema_cls)}
        unknown = set(payload.keys()) - allowed
        if unknown:
            raise SchemaValidationError(
                f"Unexpected fields for {schema_cls.__name__}: {sorted(unknown)}"
            )
        try:
            return schema_cls(**payload)
        except TypeError as exc:
            raise SchemaValidationError(str(exc)) from exc

    raise SchemaValidationError(f"Unsupported schema class: {schema_cls}")
