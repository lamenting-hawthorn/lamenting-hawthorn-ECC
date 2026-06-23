#!/usr/bin/env python3
"""Phase 6 test: Lightweight graph memory (entity extraction + edges + expansion)."""

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
from src.graph_memory import GraphMemory, _extract_entities, _infer_relationships


def _postgres_available() -> bool:
    try:
        with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql:///agent_memory")):
            return True
    except Exception:
        return False


def test_extract_entities() -> None:
    text = "Mo Memory uses Postgres and pgvector. Client A prefers weekly reports."
    entities = _extract_entities(text)
    assert "Mo Memory" in entities or "Postgres" in entities or "pgvector" in entities
    print(f"OK: extracted {len(entities)} entities: {entities}")


def test_infer_relationships() -> None:
    text = "Mo Memory uses Postgres and pgvector for vector search."
    entities = sorted(_extract_entities(text))
    edges = _infer_relationships(text, entities)
    assert edges, "Expected at least one inferred relationship"
    print(f"OK: inferred {len(edges)} relationships:")
    for src, tgt, etype, weight in edges:
        print(f"   {src} --[{etype}]--> {tgt} (weight={weight})")


def test_extract_and_link() -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available")
    gm = GraphMemory()
    # Insert a memory first
    store = DurableMemoryStore()
    mid = store.insert_memory(
        MemoryInput(
            content="The owner works on Mo Memory which uses Postgres with pgvector.",
            user_id="test_graph_user",
            session_id="test_graph_session",
            org_id="test_org",
            role="owner",
            memory_type="semantic",
            category="fact",
            visibility="owner_only",
            confidence=0.9,
            source="system_generated",
        )
    )
    edge_ids = gm.extract_and_link(mid, "The owner works on Mo Memory which uses Postgres with pgvector.")
    print(f"OK: created {len(edge_ids)} edges for memory {mid}")
    return mid, edge_ids


def test_get_related_memories() -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available")
    gm = GraphMemory()
    # First ensure we have a memory with extractable entities
    store = DurableMemoryStore()
    mid = store.insert_memory(
        MemoryInput(
            content="Postgres is a relational database. pgvector is a Postgres extension.",
            user_id="test_graph_user",
            session_id="test_graph_session",
            org_id="test_org",
            role="owner",
            memory_type="semantic",
            category="fact",
            visibility="owner_only",
            confidence=0.9,
            source="system_generated",
        )
    )
    gm.extract_and_link(mid, "Postgres is a relational database. pgvector is a Postgres extension.")

    related = gm.get_related_memories(
        mid,
        depth=1,
        limit=5,
        user_id="test_graph_user",
        org_id="test_org",
    )
    print(f"OK: found {len(related)} related memories for {mid}")
    for r in related:
        print(f"   - {r['content'][:80]}...")


def test_graph_expansion_does_not_cross_actor_scope() -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available")
    gm = GraphMemory()
    store = DurableMemoryStore()
    shared_phrase = "Private Graph Boundary Marker"
    allowed_id = store.insert_memory(
        MemoryInput(
            content=f"{shared_phrase} belongs to allowed owner.",
            user_id="graph_allowed_user",
            session_id="graph_allowed_session",
            org_id="graph_allowed_org",
            role="owner",
            visibility="owner_only",
            confidence=0.9,
            source="test",
        )
    )
    blocked_id = store.insert_memory(
        MemoryInput(
            content=f"{shared_phrase} belongs to blocked owner.",
            user_id="graph_blocked_user",
            session_id="graph_blocked_session",
            org_id="graph_blocked_org",
            role="owner",
            visibility="owner_only",
            confidence=0.9,
            source="test",
        )
    )
    gm.extract_and_link(allowed_id, shared_phrase)
    gm.extract_and_link(blocked_id, shared_phrase)

    related = gm.expand_graph(
        [allowed_id],
        depth=1,
        limit=10,
        user_id="graph_allowed_user",
        org_id="graph_allowed_org",
    )
    related_ids = {str(row["id"]) for row in related}
    assert blocked_id not in related_ids
    print("OK: graph expansion respects actor scope")


def test_edge_persistence() -> None:
    from psycopg.rows import dict_row

    if not _postgres_available():
        pytest.skip("Postgres is not available")

    url = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
    conn = psycopg.connect(url, row_factory=dict_row)
    row = conn.execute("SELECT COUNT(*) AS cnt FROM memory.memory_edges").fetchone()
    print(f"OK: memory_edges table has {row['cnt']} total edges")
    conn.close()


def main() -> None:
    print("=== Phase 6: Graph Memory Test ===\n")

    test_extract_entities()
    print()

    test_infer_relationships()
    print()

    if not _postgres_available():
        print("SKIP: Postgres is not available; DB-backed graph tests skipped.")
        print("=== Phase 6: PASSED (DB skipped) ===")
        return

    test_extract_and_link()
    print()

    test_get_related_memories()
    print()

    test_graph_expansion_does_not_cross_actor_scope()
    print()

    test_edge_persistence()
    print()

    print("=== Phase 6: PASSED ===")


if __name__ == "__main__":
    main()
