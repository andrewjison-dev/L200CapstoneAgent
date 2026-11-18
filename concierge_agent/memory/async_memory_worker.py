"""Asynchronous Background Memory Consolidation Worker.

Executes expensive post-turn memory operations (preference synthesis, semantic embedding
indexing, and pantry depletion updates) asynchronously via `asyncio.Queue` and background
tasks (`asyncio.create_task` / daemon worker thread fallback) so the Command Center UI
and agent response latency are never blocked.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
import uuid

from ..observability.structured_logger import get_structured_logger
from ..observability.tracing import get_tracer
from .persistent_store import FamilyPersistentMemoryStore, get_memory_store

logger = get_structured_logger("concierge_agent.memory.async_worker")


class AsyncMemoryConsolidationWorker:
    """Non-blocking background memory processor preventing UI stalls during consolidation."""

    def __init__(self, store: FamilyPersistentMemoryStore | None = None) -> None:
        self._store = store or get_memory_store()
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="concierge-async-mem"
        )
        self._jobs: dict[str, dict[str, Any]] = {}
        self._futures: dict[str, Future[dict[str, Any]]] = {}
        self._async_tasks: set[asyncio.Task[Any]] = set()

    def submit_background_consolidation(
        self,
        *,
        category: str,
        summary_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Schedules non-blocking memory embedding & persistence and returns `job_id` immediately."""
        job_id = f"memjob-{uuid.uuid4().hex[:10]}"
        self._jobs[job_id] = {
            "job_id": job_id,
            "status": "QUEUED",
            "category": category,
            "submitted_at_utc": datetime.now(timezone.utc).isoformat(),
        }

        # If an asyncio loop is currently running, schedule a non-blocking asyncio.Task
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._consolidate_async(
                    job_id=job_id,
                    category=category,
                    summary_text=summary_text,
                    metadata=metadata,
                )
            )
            self._async_tasks.add(task)
            task.add_done_callback(self._async_tasks.discard)
            return job_id
        except RuntimeError:
            # Outside an active asyncio loop (e.g. sync HTTP thread), dispatch to background pool
            fut = self._executor.submit(
                self._consolidate_sync,
                job_id,
                category,
                summary_text,
                metadata,
            )
            self._futures[job_id] = fut
            return job_id

    async def _consolidate_async(
        self,
        *,
        job_id: str,
        category: str,
        summary_text: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Runs memory consolidation off the main event loop via `asyncio.to_thread`."""
        return await asyncio.to_thread(
            self._consolidate_sync,
            job_id,
            category,
            summary_text,
            metadata,
        )

    def _consolidate_sync(
        self,
        job_id: str,
        category: str,
        summary_text: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._jobs[job_id]["status"] = "RUNNING"
        with get_tracer().start_span(
            "async_memory_consolidation",
            attributes={"job_id": job_id, "category": category},
        ):
            try:
                stored = self._store.upsert_memory_vector(
                    memory_id=f"mem-{job_id}",
                    category=category,
                    text=summary_text,
                    metadata=metadata,
                )
                self._jobs[job_id].update(
                    {
                        "status": "COMPLETED",
                        "memory_id": stored["memory_id"],
                        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    }
                )
                logger.info(
                    "Async memory consolidation finished",
                    extra={"structured_data": self._jobs[job_id]},
                )
                return self._jobs[job_id]
            except Exception as exc:
                self._jobs[job_id].update(
                    {
                        "status": "FAILED",
                        "error": str(exc),
                    }
                )
                logger.error(
                    "Async memory consolidation failed",
                    extra={"structured_data": self._jobs[job_id]},
                )
                raise

    async def flush_pending_async(self, timeout_seconds: float = 5.0) -> None:
        """Awaits all queued background memory jobs (used in tests and graceful shutdown)."""
        if self._async_tasks:
            await asyncio.wait(self._async_tasks, timeout=timeout_seconds)
        for fut in list(self._futures.values()):
            await asyncio.to_thread(fut.result, timeout_seconds)

    def get_job_status(self, job_id: str) -> dict[str, Any] | None:
        """Returns current status of a background consolidation job without blocking."""
        return self._jobs.get(job_id)

    def list_recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Lists recent background memory jobs for observability."""
        return list(self._jobs.values())[-limit:]


_DEFAULT_WORKER: AsyncMemoryConsolidationWorker | None = None


def get_async_memory_worker() -> AsyncMemoryConsolidationWorker:
    """Returns the singleton AsyncMemoryConsolidationWorker."""
    global _DEFAULT_WORKER
    if _DEFAULT_WORKER is None:
        _DEFAULT_WORKER = AsyncMemoryConsolidationWorker()
    return _DEFAULT_WORKER
