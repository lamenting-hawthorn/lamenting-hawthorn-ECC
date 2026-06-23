#!/usr/bin/env python3
"""Phase 5 test: Hybrid retrieval (pgvector + FTS + RRF fusion)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.durable_memory import DurableMemoryStore, MemoryInput
from src.hybrid_retrieval import HybridMemoryStore, generate_embedding


def _postgres_available() -> bool:
    try:
        with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql:///agent_memory")):
            return True
    except Exception:
        return False


def test_generate_embedding() -> None:
    emb = generate_embedding("Mo Memory uses Postgres and pgvector.")
    if emb is None:
        print("SKIP: local embedding model unavailable; embedding generation skipped.")
        return
    assert len(emb) == 1536, f"Expected 1536 dims, got {len(emb)}"
    print(f"OK: embedding generated ({len(emb)} dims)")


def test_api_embedding_provider_without_key_returns_none() -> None:
    old_provider = os.environ.get("EMBEDDING_PROVIDER")
    old_key = os.environ.get("EMBEDDING_API_KEY")
    os.environ["EMBEDDING_PROVIDER"] = "openai"
    os.environ.pop("EMBEDDING_API_KEY", None)
    try:
        assert generate_embedding("test") is None
    finally:
        if old_provider is None:
            os.environ.pop("EMBEDDING_PROVIDER", None)
        else:
            os.environ["EMBEDDING_PROVIDER"] = old_provider
        if old_key is None:
            os.environ.pop("EMBEDDING_API_KEY", None)
        else:
            os.environ["EMBEDDING_API_KEY"] = old_key
    print("OK: API embedding provider requires explicit key")


def insert_with_embedding() -> str:
    store = DurableMemoryStore()
    mid = store.insert_memory(
        MemoryInput(
            content="Mo Memory uses Python, LangGraph, and Postgres with pgvector.",
            user_id="test_hybrid_user",
            session_id="test_hybrid_session",
            org_id="test_org",
            role="owner",
            memory_type="semantic",
            category="fact",
            visibility="owner_only",
            confidence=0.95,
            source="system_generated",
        )
    )
    print(f"OK: inserted memory {mid}")
    return mid


def test_insert_with_embedding() -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available")
    assert insert_with_embedding()


def check_hybrid_search_fts(query: str, expected_substring: str) -> None:
    """Test that hybrid search returns relevant results via FTS (no API key needed)."""
    store = HybridMemoryStore()
    results = store.hybrid_search(
        query,
        user_id="test_hybrid_user",
        org_id="test_org",
        limit=5,
    )
    assert results, f"No results for query: {query!r}"
    contents = " ".join(r["content"] for r in results)
    assert expected_substring.lower() in contents.lower(), (
        f"Expected {expected_substring!r} in results, got: {contents!r}"
    )
    print(f"OK: hybrid_search('{query}') -> found {len(results)} results")
    for r in results:
        print(f"   - {r['content'][:80]}...")


def test_hybrid_search_fts() -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available")
    check_hybrid_search_fts("Mo Memory Python Postgres", "Mo Memory")


def check_hybrid_search_vector(query: str, expected_substring: str) -> None:
    """Test vector search when an embedding provider is available."""
    if generate_embedding(query) is None:
        print("SKIP: no embedding provider available; vector search not tested.")
        return
    store = HybridMemoryStore()
    results = store.hybrid_search(
        query,
        user_id="test_hybrid_user",
        org_id="test_org",
        limit=5,
    )
    assert results, f"No results for query: {query!r}"
    contents = " ".join(r["content"] for r in results)
    assert expected_substring.lower() in contents.lower()
    print(f"OK: hybrid_search('{query}') vector path -> found {len(results)} results")


def test_retrieval_logging() -> None:
    """Verify retrieval_logs table has entries after hybrid search."""
    from psycopg.rows import dict_row

    if not _postgres_available():
        pytest.skip("Postgres is not available")

    url = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
    conn = psycopg.connect(url, row_factory=dict_row)
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM memory.retrieval_logs
        WHERE retrieval_method IN ('hybrid', 'fts_only')
        """
    ).fetchone()
    assert row and row["cnt"] > 0, "Expected retrieval_logs entries for hybrid searches"
    print(f"OK: retrieval_logs has {row['cnt']} hybrid/fts_only entries")
    conn.close()


def main() -> None:
    print("=== Phase 5: Hybrid Retrieval Test ===\n")

    test_generate_embedding()
    print()

    test_api_embedding_provider_without_key_returns_none()
    print()

    if not _postgres_available():
        print("SKIP: Postgres is not available; DB-backed hybrid tests skipped.")
        print("=== Phase 5: PASSED (DB skipped) ===")
        return

    insert_with_embedding()
    print()

    # FTS search (works without API key)
    check_hybrid_search_fts("Mo Memory Python Postgres", "Mo Memory")
    print()

    # Vector search (only if API key set)
    check_hybrid_search_vector("what database does the agent use", "Postgres")
    print()

    test_retrieval_logging()
    print()

    print("=== Phase 5: PASSED ===")


if __name__ == "__main__":
    main()
