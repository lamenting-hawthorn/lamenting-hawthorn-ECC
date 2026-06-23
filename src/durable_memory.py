"""Phase 3 durable memory access.

This module writes product memory to Postgres. It is separate from LangGraph
checkpointing: checkpoints store graph state, while `memory.typed_memory`
stores semantic/episodic/procedural product memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from time import perf_counter
from typing import Any

import psycopg
from psycopg.rows import dict_row


DEFAULT_DATABASE_URL = "postgresql:///agent_memory"


@dataclass(frozen=True)
class MemoryInput:
    content: str
    user_id: str
    session_id: str
    org_id: str
    role: str = "owner"
    memory_type: str = "semantic"
    category: str = "fact"
    visibility: str = "owner_only"
    confidence: float = 1.0
    source: str = "system_generated"
    summary: str | None = None
    metadata: dict[str, Any] | None = None


class DurableMemoryStore:
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

    def _connect(self):
        conn = psycopg.connect(self.database_url, row_factory=dict_row)
        conn.execute("SELECT set_config('app.current_role', 'service', false)")
        return conn

    def insert_memory(self, memory: MemoryInput) -> str:
        with self._connect() as conn:
            # Generate embedding if content is substantial enough
            embedding_str: str | None = None
            if len(memory.content) > 10:
                try:
                    from src.hybrid_retrieval import generate_embedding
                    emb = generate_embedding(memory.content)
                    if emb:
                        embedding_str = str(emb)
                except Exception:
                    pass

            row = conn.execute(
                """
                INSERT INTO memory.typed_memory
                    (memory_type, category, content, summary, user_id, session_id,
                     org_id, role, visibility, confidence, source, metadata, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                RETURNING id
                """,
                (
                    memory.memory_type,
                    memory.category,
                    memory.content,
                    memory.summary,
                    memory.user_id,
                    memory.session_id,
                    memory.org_id,
                    memory.role,
                    memory.visibility,
                    memory.confidence,
                    memory.source,
                    psycopg.types.json.Jsonb(memory.metadata or {}),
                    embedding_str,
                ),
            ).fetchone()
            return str(row["id"])

    def search_memory_basic(
        self,
        query: str,
        *,
        user_id: str,
        session_id: str,
        org_id: str | None = None,
        limit: int = 5,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        start = perf_counter()
        pattern = f"%{query}%"

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, category, content, summary, confidence,
                       source, visibility, created_at
                FROM memory.typed_memory
                WHERE user_id = %s
                  AND (%s::text IS NULL OR org_id = %s::text OR visibility = 'owner_only')
                  AND (
                      content ILIKE %s
                      OR coalesce(summary, '') ILIKE %s
                  )
                ORDER BY confidence DESC, created_at DESC
                LIMIT %s
                """,
                (user_id, org_id, org_id, pattern, pattern, limit),
            ).fetchall()

            latency_ms = int((perf_counter() - start) * 1000)
            self.log_retrieval(
                query=query,
                user_id=user_id,
                session_id=session_id,
                results=rows,
                latency_ms=latency_ms,
                trace_id=trace_id,
            )
            return [dict(row) for row in rows]

    def log_retrieval(
        self,
        *,
        query: str,
        user_id: str,
        session_id: str,
        results: list[dict[str, Any]],
        latency_ms: int,
        trace_id: str | None = None,
    ) -> None:
        memory_ids = [row["id"] for row in results]
        relevance_scores = [float(row.get("confidence") or 0.0) for row in results]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory.retrieval_logs
                    (query, retrieval_method, results_count, memory_ids,
                     relevance_scores, latency_ms, user_id, session_id,
                     trace_id, context_used)
                VALUES
                    (%s, 'direct_lookup', %s, %s, %s, %s, %s, %s, %s, true)
                """,
                (
                    query,
                    len(results),
                    memory_ids,
                    relevance_scores,
                    latency_ms,
                    user_id,
                    session_id,
                    trace_id,
                ),
            )


def insertMemory(memory: MemoryInput) -> str:
    return DurableMemoryStore().insert_memory(memory)


def searchMemoryBasic(query: str, **kwargs) -> list[dict[str, Any]]:
    return DurableMemoryStore().search_memory_basic(query, **kwargs)


def logRetrieval(**kwargs) -> None:
    return DurableMemoryStore().log_retrieval(**kwargs)
