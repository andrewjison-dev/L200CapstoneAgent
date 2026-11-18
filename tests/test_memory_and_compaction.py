"""Tests for Category 2: Context & Memory (Constitution, Compaction, Persistent DB, Async Memory)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from concierge_agent.memory import (
    AsyncMemoryConsolidationWorker,
    ContextCompactionManager,
    FamilyPersistentMemoryStore,
    build_adk_context_cache_config,
    build_adk_events_compaction_config,
)
from concierge_agent.prompts import COORDINATOR_SYSTEM_CONSTITUTION


class TestContextAndMemory(unittest.TestCase):
    def test_system_constitution_defines_persona_schedule_and_invariants(self) -> None:
        self.assertIn("05:00 AM", COORDINATOR_SYSTEM_CONSTITUTION)
        self.assertIn("08:00 AM", COORDINATOR_SYSTEM_CONSTITUTION)
        self.assertIn("INTAKE_FIRST_PROTOCOL", COORDINATOR_SYSTEM_CONSTITUTION)
        self.assertIn("SAFETY_ALLERGEN_AND_HITL_INVARIANTS", COORDINATOR_SYSTEM_CONSTITUTION)

    def test_history_compaction_and_adk_configs(self) -> None:
        comp_cfg = build_adk_events_compaction_config()
        cache_cfg = build_adk_context_cache_config()
        self.assertIsNotNone(comp_cfg)
        self.assertIsNotNone(cache_cfg)

        compactor = ContextCompactionManager(max_token_budget=250, retain_recent_turns=3)
        bloated_history = [
            {"role": "user", "content": f"Turn {i}: " + ("meal prep details " * 15)}
            for i in range(12)
        ]
        compacted, stats = compactor.compact_conversation_history(bloated_history)
        self.assertTrue(stats["compacted"])
        self.assertLess(len(compacted), len(bloated_history))
        self.assertIn("COMPACTED_HISTORY_SUMMARY", compacted[0]["content"])

    def test_persistent_sqlite_vector_store_and_async_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_memory.db"
            store = FamilyPersistentMemoryStore(db_path=db_path)

            worker = AsyncMemoryConsolidationWorker(store=store)
            job_id = worker.submit_background_consolidation(
                category="kid_preference",
                summary_text="Kids devoured the crispy panko cod tenders with sweet peas.",
                metadata={"rating": 5},
            )
            self.assertTrue(job_id.startswith("memjob-"))
            asyncio.run(worker.flush_pending_async())

            status = worker.get_job_status(job_id)
            self.assertIsNotNone(status)
            self.assertEqual(status["status"], "COMPLETED")

            # Verify persistence across a fresh store instance pointed at the same SQLite file
            reopened = FamilyPersistentMemoryStore(db_path=db_path)
            hits = reopened.semantic_search_memories("crispy panko cod tenders", top_k=3)
            self.assertTrue(any("panko cod" in h["text"].lower() for h in hits))


if __name__ == "__main__":
    unittest.main()
