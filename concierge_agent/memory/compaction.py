"""History Compaction & Context Caching Management.

Prevents context bloat across multi-week meal planning sessions using:
  1. Native Google ADK `EventsCompactionConfig` (sliding-window + token-threshold triggers).
  2. Native Google ADK `ContextCacheConfig` (Gemini prompt prefix caching across turns).
  3. Deterministic `ContextCompactionManager` implementing token-budget truncation,
     sliding-window retention, and rolling summary consolidation for `before_model_callback`.
"""

from __future__ import annotations

from typing import Any

from ..observability.structured_logger import get_structured_logger

try:
    from google.adk.agents.context_cache_config import ContextCacheConfig  # type: ignore[import-not-found]
    from google.adk.apps.app import EventsCompactionConfig  # type: ignore[import-not-found]

    HAS_ADK_COMPACTION = True
except ImportError:  # pragma: no cover
    HAS_ADK_COMPACTION = False
    ContextCacheConfig = None  # type: ignore[assignment,misc]
    EventsCompactionConfig = None  # type: ignore[assignment,misc]

logger = get_structured_logger("concierge_agent.memory.compaction")


def build_adk_events_compaction_config(
    *,
    compaction_interval: int = 4,
    overlap_size: int = 1,
    token_threshold: int = 12000,
    event_retention_size: int = 6,
) -> Any:
    """Builds ADK `EventsCompactionConfig` combining sliding-window and token-threshold triggers."""
    if HAS_ADK_COMPACTION and EventsCompactionConfig is not None:
        return EventsCompactionConfig(
            compaction_interval=compaction_interval,
            overlap_size=overlap_size,
            token_threshold=token_threshold,
            event_retention_size=event_retention_size,
        )
    return {
        "type": "EventsCompactionConfig",
        "compaction_interval": compaction_interval,
        "overlap_size": overlap_size,
        "token_threshold": token_threshold,
        "event_retention_size": event_retention_size,
    }


def build_adk_context_cache_config(
    *,
    cache_intervals: int = 15,
    ttl_seconds: int = 1800,
    min_tokens: int = 2048,
) -> Any:
    """Builds ADK `ContextCacheConfig` to cache system instructions and static recipe catalogs."""
    if HAS_ADK_COMPACTION and ContextCacheConfig is not None:
        return ContextCacheConfig(
            cache_intervals=cache_intervals,
            ttl_seconds=ttl_seconds,
            min_tokens=min_tokens,
        )
    return {
        "type": "ContextCacheConfig",
        "cache_intervals": cache_intervals,
        "ttl_seconds": ttl_seconds,
        "min_tokens": min_tokens,
    }


class ContextCompactionManager:
    """Manages sliding-window history compaction and token-threshold summarization."""

    def __init__(
        self,
        *,
        max_token_budget: int = 8000,
        retain_recent_turns: int = 6,
        overlap_turns: int = 1,
    ) -> None:
        self.max_token_budget = max_token_budget
        self.retain_recent_turns = retain_recent_turns
        self.overlap_turns = overlap_turns

    @staticmethod
    def estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """Approximates prompt token count (~4 chars/token heuristic)."""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return max(1, total_chars // 4)

    def compact_conversation_history(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Compacts older conversational turns into a rolling summary when exceeding budget.

        Args:
            messages: Chronological list of `{"role": str, "content": str}` turns.

        Returns:
            Tuple of `(compacted_messages, compaction_telemetry_dict)`.
        """
        original_tokens = self.estimate_tokens(messages)
        if (
            len(messages) <= self.retain_recent_turns
            and original_tokens <= self.max_token_budget
        ):
            return list(messages), {
                "compacted": False,
                "original_turn_count": len(messages),
                "retained_turn_count": len(messages),
                "original_tokens": original_tokens,
                "compacted_tokens": original_tokens,
            }

        split_idx = max(1, len(messages) - self.retain_recent_turns)
        older_slice = messages[: split_idx + self.overlap_turns]
        recent_slice = messages[split_idx:]

        # Summarize key decisions, dietary notes, and approved selections from older turns
        highlights: list[str] = []
        for turn in older_slice:
            role = turn.get("role", "user")
            raw_text = " ".join(str(turn.get("content", "")).split())
            snippet = raw_text[:140] + ("..." if len(raw_text) > 140 else "")
            if snippet:
                highlights.append(f"[{role}] {snippet}")

        summary_message = {
            "role": "system",
            "content": (
                "COMPACTED_HISTORY_SUMMARY (older turns summarized to preserve token budget):\n"
                + "\n".join(f"- {item}" for item in highlights[-10:])
            ),
        }
        compacted = [summary_message, *recent_slice]

        # Enforce hard token ceiling if single messages are still oversized
        while len(compacted) > 2 and self.estimate_tokens(compacted) > self.max_token_budget:
            compacted.pop(1)

        compacted_tokens = self.estimate_tokens(compacted)
        telemetry = {
            "compacted": True,
            "original_turn_count": len(messages),
            "retained_turn_count": len(compacted),
            "original_tokens": original_tokens,
            "compacted_tokens": compacted_tokens,
        }
        logger.info(
            "Compacted session conversation history",
            extra={"structured_data": telemetry},
        )
        return compacted, telemetry
