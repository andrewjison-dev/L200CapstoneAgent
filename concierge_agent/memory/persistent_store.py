"""Persistent Session & Vector Memory Store (SQLite + Vertex AI Search / Memory Bank).

Provides durable cross-turn storage for:
  1. Conversational session state (`DatabaseSessionService` in ADK + SQLite persistence).
  2. Semantic preference & recipe memory retrieval via cosine-similarity vector embeddings
     locally, plus Vertex AI Search (`discoveryengine`) / `VertexAiMemoryBankService` in cloud mode.
  3. Pantry inventory, staged weekly meal plans, and staged grocery carts.

All text persisted to disk passes through `PIIRedactionPipeline` prior to storage.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Any

from ..config.settings import AgentSettings, get_settings
from ..observability.pii_redactor import get_pii_redactor
from ..observability.structured_logger import get_structured_logger

logger = get_structured_logger("concierge_agent.memory.persistent_store")


def build_adk_session_service(settings: AgentSettings | None = None) -> Any:
    """Builds an ADK persistent `DatabaseSessionService` (falling back cleanly if `db` extra absent)."""
    cfg = settings or get_settings()
    cfg.local_state_dir.mkdir(parents=True, exist_ok=True)
    try:
        from google.adk.sessions import DatabaseSessionService  # type: ignore[import-not-found]

        return DatabaseSessionService(db_url=cfg.session_database_url)
    except Exception:
        try:
            from google.adk.sessions import InMemorySessionService  # type: ignore[import-not-found]

            return InMemorySessionService()
        except Exception:
            return None


def build_adk_memory_service(settings: AgentSettings | None = None) -> Any:
    """Builds ADK `VertexAiMemoryBankService` in cloud mode or `InMemoryMemoryService` in isolated mode."""
    cfg = settings or get_settings()
    if (
        cfg.is_cloud_enabled
        and not cfg.vertex_memory_bank_agent_engine_id.startswith("{{PLACEHOLDER_")
    ):
        try:
            from google.adk.memory import VertexAiMemoryBankService  # type: ignore[import-not-found]

            return VertexAiMemoryBankService(
                project=cfg.gcp_project_id,
                location=cfg.gcp_location,
                agent_engine_id=cfg.vertex_memory_bank_agent_engine_id,
            )
        except Exception:
            pass
    try:
        from google.adk.memory.in_memory_memory_service import (  # type: ignore[import-not-found]
            InMemoryMemoryService,
        )

        return InMemoryMemoryService()
    except Exception:
        return None


def _compute_deterministic_embedding(text: str, dims: int = 64) -> list[float]:
    """Computes a normalized bag-of-ngrams hashed embedding vector for local vector search."""
    vec = [0.0] * dims
    tokens = [tok.strip(".,!?;:()[]\"'").lower() for tok in text.split() if tok.strip()]
    if not tokens:
        return vec
    features = list(tokens) + [
        f"{tokens[i]}_{tokens[i + 1]}" for i in range(len(tokens) - 1)
    ]
    for feat in features:
        digest = hashlib.sha256(feat.encode("utf-8")).digest()
        idx = digest[0] % dims
        sign = 1.0 if (digest[1] % 2 == 0) else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [round(v / norm, 6) for v in vec]
    return vec


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    return sum(a * b for a, b in zip(vec_a, vec_b))


class FamilyPersistentMemoryStore:
    """SQLite + Vector Store managing Andrew's household profile, pantry, plans, and memories."""

    def __init__(self, db_path: Path | None = None, settings: AgentSettings | None = None) -> None:
        self._settings = settings or get_settings()
        if db_path is None:
            self._settings.local_state_dir.mkdir(parents=True, exist_ok=True)
            db_path = self._settings.local_state_dir / "family_memory_vector.db"
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.RLock()
        self._initialize_schema_and_seed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_schema_and_seed(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS household_kv_state (
                    state_key TEXT PRIMARY KEY,
                    json_value TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversational_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    scrubbed_content TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS semantic_memory_vectors (
                    memory_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    scrubbed_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                """
            )

            existing = conn.execute(
                "SELECT state_key FROM household_kv_state WHERE state_key = 'family_profile'"
            ).fetchone()
            if existing is None:
                default_profile = {
                    "primary_user": "Andrew",
                    "household_members": {
                        "adults": 2,
                        "young_children": 2,
                        "notes": "Spouse and 2 young kids with strict evening dinner & bedtime routines.",
                    },
                    "timezone": "America/Chicago",
                    "daily_routine_cst": {
                        "morning_workout": "05:00-06:00 CST",
                        "family_breakfast_prep": "06:00-06:40 CST",
                        "work_block": "08:00-17:00 CST",
                        "family_dinner_and_memories": "17:15-20:00 CST",
                    },
                    "excluded_allergens": ["peanuts", "tree_nuts"],
                    "nutrition_targets_andrew": {
                        "min_breakfast_protein_g": 32,
                        "daily_protein_target_g": 165,
                        "max_weeknight_cook_minutes": 25,
                    },
                    "default_weekly_budget_usd": self._settings.default_weekly_budget_usd,
                    "pantry_inventory": {
                        "rolled oats": {"quantity": 2.0, "unit": "lbs"},
                        "chia seeds": {"quantity": 1.0, "unit": "bag"},
                        "extra virgin olive oil": {"quantity": 1.0, "unit": "bottle"},
                        "low-sodium soy sauce": {"quantity": 1.0, "unit": "bottle"},
                        "jasmine rice": {"quantity": 4.0, "unit": "lbs"},
                        "garlic cloves": {"quantity": 1.0, "unit": "bulb"},
                        "honey": {"quantity": 1.0, "unit": "jar"},
                    },
                }
                now_iso = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO household_kv_state (state_key, json_value, updated_at_utc) VALUES (?, ?, ?)",
                    ("family_profile", json.dumps(default_profile), now_iso),
                )

                # Seed initial episodic family meal memories
                seed_memories = [
                    (
                        "mem-seed-1",
                        "kid_preference",
                        "Both young kids love mild teriyaki salmon bites, taco bowls with Mild cheddar, and hidden-veggie turkey meatballs when spice is added separately for adults.",
                        {"rating": 5},
                    ),
                    (
                        "mem-seed-2",
                        "morning_routine",
                        "Post-5AM workout breakfasts work best when overnight protein oats or egg-white veggie muffins are batch-prepped on Sunday so 6AM assembly takes under 8 minutes.",
                        {"rating": 5},
                    ),
                ]
                for mem_id, cat, text, meta in seed_memories:
                    emb = _compute_deterministic_embedding(text)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO semantic_memory_vectors
                        (memory_id, category, scrubbed_text, embedding_json, metadata_json, updated_at_utc)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (mem_id, cat, text, json.dumps(emb), json.dumps(meta), now_iso),
                    )
            conn.commit()

    def get_state(self, state_key: str, default: Any = None) -> Any:
        """Reads a JSON value from persistent SQLite key-value state."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT json_value FROM household_kv_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
            if row is None:
                return default
            return json.loads(row["json_value"])

    def set_state(self, state_key: str, value: Any) -> None:
        """Scrubs PII and persists a JSON value into SQLite key-value state."""
        redactor = get_pii_redactor()
        scrubbed = redactor.scrub_structure(value)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO household_kv_state (state_key, json_value, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    json_value = excluded.json_value,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (state_key, json.dumps(scrubbed), now_iso),
            )
            conn.commit()

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        """Persists a PII-scrubbed conversational turn for cross-session continuity."""
        scrubbed_content = get_pii_redactor().scrub_text(content)
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversational_turns (session_id, role, scrubbed_content, created_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, scrubbed_content, now_iso),
            )
            conn.commit()

    def get_session_turns(self, session_id: str, limit: int = 50) -> list[dict[str, str]]:
        """Loads persisted conversational turns for `session_id`."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, scrubbed_content, created_at_utc
                FROM conversational_turns
                WHERE session_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            {
                "role": r["role"],
                "content": r["scrubbed_content"],
                "created_at_utc": r["created_at_utc"],
            }
            for r in rows
        ]

    def upsert_memory_vector(
        self,
        *,
        memory_id: str,
        category: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Scrubs PII, computes vector embedding, and stores semantic memory."""
        redactor = get_pii_redactor()
        scrubbed_text = redactor.scrub_text(text)
        scrubbed_meta = redactor.scrub_structure(metadata or {})
        embedding = _compute_deterministic_embedding(scrubbed_text)
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO semantic_memory_vectors
                (memory_id, category, scrubbed_text, embedding_json, metadata_json, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    category = excluded.category,
                    scrubbed_text = excluded.scrubbed_text,
                    embedding_json = excluded.embedding_json,
                    metadata_json = excluded.metadata_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    memory_id,
                    category,
                    scrubbed_text,
                    json.dumps(embedding),
                    json.dumps(scrubbed_meta),
                    now_iso,
                ),
            )
            conn.commit()

        return {
            "memory_id": memory_id,
            "category": category,
            "scrubbed_text": scrubbed_text,
            "metadata": scrubbed_meta,
        }

    def semantic_search_memories(
        self,
        query: str,
        *,
        top_k: int = 5,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieves top-K relevant memories via Vertex AI Search (cloud) or vector cosine similarity."""
        if (
            self._settings.is_cloud_enabled
            and not self._settings.vertex_search_datastore_id.startswith("{{PLACEHOLDER_")
        ):
            cloud_hits = self._search_vertex_ai_datastore(query, top_k=top_k)
            if cloud_hits:
                return cloud_hits

        query_vec = _compute_deterministic_embedding(query)
        with self._lock, self._connect() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM semantic_memory_vectors WHERE category = ?",
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM semantic_memory_vectors").fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            emb = json.loads(row["embedding_json"])
            score = _cosine_similarity(query_vec, emb)
            scored.append(
                {
                    "memory_id": row["memory_id"],
                    "category": row["category"],
                    "text": row["scrubbed_text"],
                    "similarity_score": round(score, 4),
                    "metadata": json.loads(row["metadata_json"]),
                }
            )
        scored.sort(key=lambda item: item["similarity_score"], reverse=True)
        return scored[:top_k]

    def _search_vertex_ai_datastore(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Queries Vertex AI Search (`google-cloud-discoveryengine`) when cloud datastore is configured."""
        try:
            from google.cloud import discoveryengine_v1 as discoveryengine  # type: ignore[import-untyped]

            client = discoveryengine.SearchServiceClient()
            serving_config = (
                f"projects/{self._settings.gcp_project_id}/locations/global/"
                f"collections/default_collection/dataStores/"
                f"{self._settings.vertex_search_datastore_id}/servingConfigs/default_search"
            )
            request = discoveryengine.SearchRequest(
                serving_config=serving_config,
                query=query,
                page_size=top_k,
            )
            response = client.search(request)
            results: list[dict[str, Any]] = []
            for idx, result in enumerate(response.results):
                doc_data = dict(result.document.derived_struct_data)
                results.append(
                    {
                        "memory_id": result.document.id or f"vertex-{idx}",
                        "category": "vertex_ai_search",
                        "text": str(doc_data.get("snippets", query)),
                        "similarity_score": 1.0,
                        "metadata": {"source": "vertex_ai_search"},
                    }
                )
            return results
        except Exception as exc:
            logger.warning(
                "Vertex AI Search unavailable; falling back to local SQLite vector store",
                extra={"structured_data": {"error": str(exc)}},
            )
            return []


_DEFAULT_STORE: FamilyPersistentMemoryStore | None = None


def get_memory_store() -> FamilyPersistentMemoryStore:
    """Returns the singleton FamilyPersistentMemoryStore."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = FamilyPersistentMemoryStore()
    return _DEFAULT_STORE
