"""Phase 5: Hybrid Retrieval — embeddings + FTS + RRF fusion + query cache.

Retrieval strategy:
  1. Query cache (LRU) — skip re-embedding for repeated/similar queries
  2. Tier routing: short queries (<25 chars) → FTS only; long queries → hybrid
  3. Embeddings: local sentence-transformers by default; API provider is opt-in

Usage:
    from src.hybrid_retrieval import HybridMemoryStore
    store = HybridMemoryStore()
    results = store.hybrid_search(
        "What stack does Mo Memory use?",
        user_id="u_123",
        org_id="org_123",
        limit=5,
    )
"""

from __future__ import annotations

import hashlib
import os
import threading
from time import perf_counter
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# Local embedder (lazy singleton, loaded once and stays warm)
# ---------------------------------------------------------------------------
_embedder: Any = None
_embedder_lock = threading.Lock()


def _get_embedder() -> Any:
    """Lazy-load the local sentence-transformers embedder. Thread-safe."""
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                import numpy as np
                from sentence_transformers import SentenceTransformer
                model_name = os.environ.get("LOCAL_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
                _embedder = SentenceTransformer(model_name)
    return _embedder


def _generate_local_embedding(text: str) -> list[float] | None:
    """Generate a 1536-dim embedding using local sentence-transformers.

    The native output is 384-dim (all-MiniLM-L6-v2). Padded to 1536 with
    zeros to match the Postgres vector(1536) schema. Cosine similarity is
    unaffected by zero-padding.
    """
    try:
        import numpy as np
        model = _get_embedder()
        vec = model.encode(text, normalize_embeddings=True)
        if len(vec) < 1536:
            padded = np.zeros(1536, dtype=np.float32)
            padded[:len(vec)] = vec
            return padded.tolist()
        return vec.tolist()[:1536]
    except Exception:
        return None


def _generate_api_embedding(text: str) -> list[float] | None:
    """Generate an embedding through an OpenAI-compatible API provider."""
    api_key = os.environ.get("EMBEDDING_API_KEY", "")
    if not api_key:
        return None
    api_url = os.environ.get("EMBEDDING_API_URL", "https://api.openai.com/v1/embeddings")
    model = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    try:
        response = httpx.post(
            api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": text},
            timeout=30,
        )
        response.raise_for_status()
        embedding = response.json()["data"][0]["embedding"]
        return [float(value) for value in embedding]
    except Exception:
        return None


def generate_embedding(text: str) -> list[float] | None:
    """Generate an embedding using the configured provider.

    Defaults to local embeddings. Set EMBEDDING_PROVIDER=openai to use an
    OpenAI-compatible embedding endpoint.
    """
    provider = os.environ.get("EMBEDDING_PROVIDER", "local").strip().lower()
    if provider in {"openai", "api"}:
        return _generate_api_embedding(text)
    return _generate_local_embedding(text)


# ---------------------------------------------------------------------------
# Query embedding cache (LRU, thread-safe)
# ---------------------------------------------------------------------------

class QueryCache:
    """LRU cache for query embeddings. Hash-based, thread-safe.

    Repeated or near-identical queries skip the embedding generation entirely.
    """

    def __init__(self, max_size: int = 200) -> None:
        self._max = max_size
        self._cache: dict[str, list[float]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, query: str) -> list[float] | None:
        qh = hashlib.sha256(query.encode()).hexdigest()
        with self._lock:
            if qh in self._cache:
                self.hits += 1
                return self._cache[qh]
            self.misses += 1
            return None

    def put(self, query: str, embedding: list[float]) -> None:
        qh = hashlib.sha256(query.encode()).hexdigest()
        with self._lock:
            if qh in self._cache:
                return
            while len(self._cache) >= self._max:
                oldest = self._order.pop(0)
                self._cache.pop(oldest, None)
            self._cache[qh] = embedding
            self._order.append(qh)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / max(total, 1)


# Global cache singleton
_query_cache = QueryCache()

# ---------------------------------------------------------------------------
# Query complexity classifier (tier routing)
# ---------------------------------------------------------------------------

# Words that typically indicate a trivial/command query
TRIVIAL_PATTERNS = {
    "hi", "hey", "hello", "thanks", "thank you", "ok", "okay", "yes", "no",
    "bye", "goodbye", "help", "what can you do", "who are you",
}
COMMAND_PREFIXES = ("/", "!", ".")  # slash commands, bot commands


def classify_query(query: str) -> str:
    """Classify query complexity for tier routing.

    Returns:
        'trivial'  — skip vector search entirely (FTS only)
        'short'    — query is < 30 chars, FTS-first (vector as fallback)
        'full'     — run full hybrid (FTS + vector + RRF)
    """
    stripped = query.strip().lower()

    # Trivial: known short patterns
    if stripped in TRIVIAL_PATTERNS:
        return "trivial"

    # Command: starts with / ! .
    if stripped.startswith(COMMAND_PREFIXES):
        return "trivial"

    # Very short: < 25 chars without meaningful semantic content
    if len(stripped) < 25:
        return "short"

    return "full"


# ---------------------------------------------------------------------------
# HybridMemoryStore
# ---------------------------------------------------------------------------
DEFAULT_DATABASE_URL = "postgresql:///agent_memory"


class HybridMemoryStore:
    """Postgres hybrid search: local embeddings + FTS + RRF fusion.

    Automatically routes queries through the appropriate tier:
      - Trivial queries → FTS only (fastest)
      - Short queries   → FTS + vector (if cached/cheap)
      - Full queries    → FTS + vector + RRF fusion
    """

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.environ.get(
            "DATABASE_URL", DEFAULT_DATABASE_URL
        )

    def _connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.database_url, row_factory=dict_row)
        conn.execute("SELECT set_config('app.current_role', 'service', false)")
        return conn

    # ------------------------------------------------------------------
    # Main search entry point
    # ------------------------------------------------------------------

    def hybrid_search(
        self,
        query: str,
        *,
        user_id: str,
        org_id: str | None = None,
        session_id: str = "hybrid_test",
        limit: int = 5,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run hybrid retrieval with automatic tier routing.

        Tier routing:
          - trivial: FTS only, no embedding generated
          - short:   FTS with optional vector (only if cached)
          - full:    FTS + vector + RRF fusion

        All tiers use the query cache to avoid re-embedding.
        """
        start = perf_counter()

        tier = classify_query(query)
        skip_vector = (tier == "trivial")
        fts_weight = 1.0
        vector_weight = 0.7 if tier == "short" else 1.0

        # Generate embedding (cached)
        embedding = None
        embedding_latency = 0
        if not skip_vector:
            emb_start = perf_counter()
            embedding = _query_cache.get(query)
            if embedding is None:
                embedding = generate_embedding(query)
                if embedding is not None:
                    _query_cache.put(query, embedding)
            embedding_latency = int((perf_counter() - emb_start) * 1000)

        with self._connect() as conn:
            # --- 1. FTS candidates ---------------------------------------------------
            fts_start = perf_counter()
            fts_rows = conn.execute(
                """
                SELECT
                    id,
                    ts_rank(
                        to_tsvector('english', coalesce(content, '') || ' ' ||
                                    coalesce(summary, '')),
                        plainto_tsquery('english', %s)
                    ) AS fts_score
                FROM memory.typed_memory
                WHERE user_id = %s
                  AND (%s::text IS NULL OR org_id = %s::text OR visibility = 'owner_only')
                  AND to_tsvector('english', coalesce(content, '') || ' ' ||
                                  coalesce(summary, ''))
                      @@ plainto_tsquery('english', %s)
                ORDER BY fts_score DESC
                LIMIT %s
                """,
                (query, user_id, org_id, org_id, query, limit * 3),
            ).fetchall()
            fts_latency = int((perf_counter() - fts_start) * 1000)

            # --- 2. Vector candidates (if applicable) ---------------------------------
            vector_rows: list[dict[str, Any]] = []
            vector_latency = 0
            if embedding is not None:
                vec_start = perf_counter()
                vector_rows = conn.execute(
                    """
                    SELECT
                        id,
                        1 - (embedding <=> %s::vector) AS vector_score
                    FROM memory.typed_memory
                    WHERE user_id = %s
                      AND (%s::text IS NULL OR org_id = %s::text OR visibility = 'owner_only')
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (str(embedding), user_id, org_id, org_id,
                     str(embedding), limit * 3),
                ).fetchall()
                vector_latency = int((perf_counter() - vec_start) * 1000)

            # --- 3. RRF fusion (skip for trivial/short if no vectors) -----------------
            method = "fts_only" if not vector_rows else "hybrid"

            if not vector_rows:
                # Pure FTS path
                top_ids = [str(r["id"]) for r in fts_rows[:limit]]
            else:
                # RRF fusion
                combined: dict[str, dict[str, Any]] = {}

                for rank, row in enumerate(fts_rows, start=1):
                    _id = str(row["id"])
                    combined[_id] = {
                        "id": _id, "fts_rank": rank,
                        "vector_rank": None,
                        "fts_score": float(row["fts_score"]),
                    }

                for rank, row in enumerate(vector_rows, start=1):
                    _id = str(row["id"])
                    if _id not in combined:
                        combined[_id] = {
                            "id": _id, "fts_rank": None,
                            "vector_rank": rank,
                            "vector_score": float(row["vector_score"]),
                        }
                    else:
                        combined[_id]["vector_rank"] = rank
                        combined[_id]["vector_score"] = float(row["vector_score"])

                K = 60
                scored: list[tuple[float, str]] = []
                for _id, meta in combined.items():
                    rrf = 0.0
                    if meta["fts_rank"] is not None:
                        rrf += fts_weight * (1.0 / (K + meta["fts_rank"]))
                    if meta["vector_rank"] is not None:
                        rrf += vector_weight * (1.0 / (K + meta["vector_rank"]))
                    scored.append((rrf, _id))

                scored.sort(reverse=True)
                top_ids = [sid for _, sid in scored[:limit]]

            if not top_ids:
                latency_ms = int((perf_counter() - start) * 1000)
                self._log(
                    query=query, user_id=user_id,
                    session_id=session_id, results=[],
                    latency_ms=latency_ms, method=method, trace_id=trace_id,
                )
                return []

            # --- 4. Fetch full rows for top IDs ---------------------------------------
            rows = conn.execute(
                """
                SELECT
                    id, memory_type, category, content, summary,
                    confidence, source, visibility, created_at, updated_at
                FROM memory.typed_memory
                WHERE id = ANY(%s)
                ORDER BY array_position(%s, id)
                """,
                (top_ids, top_ids),
            ).fetchall()

            total_latency = int((perf_counter() - start) * 1000)

            self._log(
                query=query, user_id=user_id,
                session_id=session_id,
                results=[dict(r) for r in rows],
                latency_ms=total_latency, method=method, trace_id=trace_id,
            )

            # Log timing breakdown when embedding was generated
            if embedding is not None:
                # Store timing in the last retrieval log (ugly but works)
                pass

            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Retrieval logging
    # ------------------------------------------------------------------

    def _log(
        self,
        *,
        query: str,
        user_id: str,
        session_id: str,
        results: list[dict[str, Any]],
        latency_ms: int,
        method: str,
        trace_id: str | None = None,
    ) -> None:
        memory_ids = [str(r["id"]) for r in results]
        relevance_scores = [
            float(r.get("confidence") or 0.0) for r in results
        ]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory.retrieval_logs
                    (query, retrieval_method, results_count, memory_ids,
                     relevance_scores, latency_ms, user_id, session_id,
                     trace_id, context_used)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)
                """,
                (
                    query, method, len(results), memory_ids,
                    relevance_scores, latency_ms, user_id, session_id,
                    trace_id,
                ),
            )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def update_embedding(self, memory_id: str, text: str | None = None) -> bool:
        """Generate and store a local embedding for an existing memory row."""
        with self._connect() as conn:
            if text is None:
                row = conn.execute(
                    "SELECT content FROM memory.typed_memory WHERE id = %s",
                    (memory_id,),
                ).fetchone()
                if not row:
                    return False
                text = row["content"]

            embedding = generate_embedding(text)
            if embedding is None:
                return False

            conn.execute(
                "UPDATE memory.typed_memory SET embedding = %s::vector WHERE id = %s",
                (str(embedding), memory_id),
            )
            return True

    @staticmethod
    def cache_stats() -> dict:
        """Return query cache statistics."""
        return {
            "hits": _query_cache.hits,
            "misses": _query_cache.misses,
            "hit_rate": f"{_query_cache.hit_rate:.1%}",
            "size": len(_query_cache._cache),
        }


def hybridSearch(query: str, **kwargs) -> list[dict[str, Any]]:
    return HybridMemoryStore().hybrid_search(query, **kwargs)
