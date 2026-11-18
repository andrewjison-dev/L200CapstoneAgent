"""Strategic Model Routing between Fast (`gemini-2.5-flash`) and Planning (`gemini-2.5-pro`) tiers.

Optimizes latency and cost across Andrew's Command Center workflow:
  - Routes deterministic intake checks, recipe filtering, grocery aisle consolidation,
    cooking timers, and guardrail classification to `gemini-2.5-flash`.
  - Routes complex multi-constraint weekly schedule synthesis, macro trade-offs, and
    multi-day batch-prep optimization to `gemini-2.5-pro`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..config.settings import AgentSettings, get_settings
from ..observability.structured_logger import get_structured_logger
from ..observability.tracing import get_tracer

logger = get_structured_logger("concierge_agent.orchestration.model_router")

_PRO_COMPLEXITY_SIGNALS = frozenset(
    {
        "weekly_meal_plan",
        "multi_constraint_optimization",
        "macro_rebalance",
        "batch_prep_architecture",
        "schedule_conflict_resolution",
    }
)


@dataclass(frozen=True)
class ModelRouteDecision:
    """Audit record explaining why a task was routed to Flash vs. Pro."""

    task_type: str
    selected_model: str
    tier: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategicModelRouter:
    """Selects the optimal Gemini model tier per sub-agent or dynamic user query."""

    def __init__(self, settings: AgentSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._routing_history: list[ModelRouteDecision] = []

    def select_model(
        self,
        *,
        task_type: str,
        user_query: str = "",
        constraint_count: int = 1,
    ) -> ModelRouteDecision:
        """Selects `gemini-2.5-pro` for deep planning or `gemini-2.5-flash` for low-latency tasks."""
        with get_tracer().start_span(
            "strategic_model_router",
            attributes={"task_type": task_type, "constraint_count": constraint_count},
        ) as span:
            lowered_query = user_query.lower()
            needs_pro_planning = (
                task_type in _PRO_COMPLEXITY_SIGNALS
                or constraint_count >= 4
                or any(
                    kw in lowered_query
                    for kw in (
                        "full week",
                        "orchestrate",
                        "rebalance macros",
                        "optimize sunday prep",
                        "7-day plan",
                    )
                )
            )

            if needs_pro_planning:
                decision = ModelRouteDecision(
                    task_type=task_type,
                    selected_model=self._settings.planning_model_name,
                    tier="PRO_REASONING",
                    rationale=(
                        "Multi-constraint 7-day schedule, macro, budget, and Sunday batch-prep "
                        "optimization routed to Pro tier."
                    ),
                )
            else:
                decision = ModelRouteDecision(
                    task_type=task_type,
                    selected_model=self._settings.fast_model_name,
                    tier="FLASH_LOW_LATENCY",
                    rationale=(
                        "Structured lookup, intake validation, aisle sorting, or step timer "
                        "execution routed to Flash tier for sub-second UX."
                    ),
                )

            span.set_attribute("selected_model", decision.selected_model)
            span.set_attribute("tier", decision.tier)
            self._routing_history.append(decision)
            logger.info(
                f"Routed {task_type} -> {decision.selected_model}",
                extra={"structured_data": decision.to_dict()},
            )
            return decision

    def get_routing_history(self, limit: int = 30) -> list[dict[str, Any]]:
        """Returns recent model routing decisions for Command Center telemetry."""
        return [d.to_dict() for d in self._routing_history[-limit:]]


_DEFAULT_ROUTER: StrategicModelRouter | None = None


def get_model_router() -> StrategicModelRouter:
    """Returns the singleton StrategicModelRouter."""
    global _DEFAULT_ROUTER
    if _DEFAULT_ROUTER is None:
        _DEFAULT_ROUTER = StrategicModelRouter()
    return _DEFAULT_ROUTER
