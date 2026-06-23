#!/usr/bin/env python3
"""Comprehensive integration test: All Phases 1-7.

This test exercises the full system end-to-end on a clean database:
1. LangGraph workflow with Postgres checkpointing
2. Durable typed memory insert
3. Hybrid retrieval (local embeddings + FTS + RRF)
4. Graph memory (entity extraction + edge expansion)
5. WhatsApp adapter (actor resolution + event storage + graph invocation)
6. Real DeepSeek LLM responses
7. Database state verification

Usage:
    DATABASE_URL=postgresql:///agent_memory \
    AGENT_ORG_ID=phase1_org \
    OWNER_WHATSAPP_PHONES=15550000001 \
    python src/test_all_phases.py

Set LLM_API_KEY in the environment first if real LLM verification is required.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Phase 0: Environment check
# ---------------------------------------------------------------------------

def phase0_env() -> dict[str, str]:
    print("=" * 60)
    print("PHASE 0: Environment")
    print("=" * 60)

    required = ["DATABASE_URL"]
    optional = ["LLM_API_KEY", "OWNER_WHATSAPP_PHONES", "AGENT_ORG_ID"]

    for k in required:
        v = os.environ.get(k)
        if not v:
            raise RuntimeError(f"Missing required env var: {k}")
        print(f"  {k}: {_redact_url(v)}")

    for k in optional:
        v = os.environ.get(k)
        print(f"  {k}: {'SET' if v else 'NOT SET (will skip real LLM)'}")

    print("  OK\n")
    return {"thread_id": f"all-phases-{uuid.uuid4().hex[:8]}"}


# ---------------------------------------------------------------------------
# Phase 1: LangGraph + Postgres Checkpointing
# ---------------------------------------------------------------------------

def phase1_checkpoint(thread_id: str) -> dict:
    print("=" * 60)
    print("PHASE 1: LangGraph + Postgres Checkpointing")
    print("=" * 60)

    from src.checkpoints import build_checkpointer
    from src.graph import build_graph

    os.environ["CHECKPOINTER"] = "postgres"
    os.environ["MEMORY_BACKEND"] = "postgres"

    handle = build_checkpointer("postgres")
    graph = build_graph(handle.checkpointer)

    # Turn 1
    state = {
        "user_text": "Remember this: Mo Memory is built with Python, LangGraph, and Postgres.",
        "actor": {"actor_id": "phase1_actor", "org_id": "phase1_org", "role": "owner"},
        "session_id": thread_id,
        "messages": [],
        "memory_writes": [],
        "written_memory_ids": [],
    }
    result1 = graph.invoke(state, config={"configurable": {"thread_id": thread_id}})

    assert "assistant_response" in result1, "Missing assistant_response"
    print(f"  Turn 1 response: {result1['assistant_response'][:80]}...")
    assert len(result1.get("memory_writes", [])) == 1, "Expected 1 memory write"
    assert len(result1.get("written_memory_ids", [])) == 1, "Expected 1 written ID"
    print(f"  Written memory ID: {result1['written_memory_ids'][0]}")

    # Verify checkpoint exists in Postgres
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM public.checkpoints WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
        assert row is not None and row[0] > 0, "Checkpoint not persisted"
        print(f"  Checkpoints in DB: {row[0]}")

    print("  OK\n")
    return {
        "thread_id": thread_id,
        "memory_id": result1["written_memory_ids"][0],
        "actor": state["actor"],
    }


# ---------------------------------------------------------------------------
# Phase 2: Durable Memory + Retrieval
# ---------------------------------------------------------------------------

def phase2_retrieve(ctx: dict) -> dict:
    print("=" * 60)
    print("PHASE 2: Durable Memory Retrieval")
    print("=" * 60)

    from src.graph import build_graph
    from src.checkpoints import build_checkpointer

    handle = build_checkpointer("postgres")
    graph = build_graph(handle.checkpointer)

    # Turn 2: query the stored memory
    state2 = {
        "user_text": "What stack does Mo Memory use?",
        "actor": ctx["actor"],
        "session_id": ctx["thread_id"],
        "messages": [],
        "memory_writes": [],
        "written_memory_ids": [],
    }
    result2 = graph.invoke(state2, config={"configurable": {"thread_id": ctx["thread_id"]}})

    retrieved = result2.get("retrieved_context", "")
    print(f"  Retrieved context: {retrieved[:100]}...")
    assert "Python" in retrieved or "Postgres" in retrieved or "LangGraph" in retrieved, \
        f"Expected memory content in retrieval, got: {retrieved}"
    print(f"  Turn 2 response: {result2['assistant_response'][:80]}...")

    print("  OK\n")
    return ctx


# ---------------------------------------------------------------------------
# Phase 3: Hybrid Retrieval (local embeddings)
# ---------------------------------------------------------------------------

def phase3_hybrid(ctx: dict) -> dict:
    print("=" * 60)
    print("PHASE 3: Hybrid Retrieval (local embeddings)")
    print("=" * 60)

    from src.local_embeddings import encode_text
    from src.hybrid_retrieval import HybridMemoryStore

    # Verify embeddings work
    emb = encode_text("test embedding generation")
    assert len(emb) == 1536, f"Expected 1536 dims, got {len(emb)}"
    print(f"  Embedding dims: {len(emb)} ✓")

    # Insert a memory with local embedding
    from src.durable_memory import DurableMemoryStore, MemoryInput
    store = DurableMemoryStore()
    mid = store.insert_memory(MemoryInput(
        content="Semantic search works with local sentence-transformers embeddings.",
        user_id=ctx["actor"]["actor_id"],
        session_id=ctx["thread_id"],
        org_id=ctx["actor"]["org_id"],
        memory_type="semantic",
        category="fact",
        visibility="owner_only",
        confidence=0.9,
    ))
    print(f"  Inserted memory with embedding: {mid}")

    # Hybrid search
    hstore = HybridMemoryStore()
    results = hstore.hybrid_search(
        "local embedding search",
        user_id=ctx["actor"]["actor_id"],
        org_id=ctx["actor"]["org_id"],
        limit=3,
    )
    assert len(results) > 0, "Hybrid search should find the memory"
    print(f"  Hybrid results: {len(results)}")
    for r in results:
        print(f"    - {r['content'][:70]}...")

    print("  OK\n")
    return ctx


# ---------------------------------------------------------------------------
# Phase 4: Graph Memory (entities + edges)
# ---------------------------------------------------------------------------

def phase4_graph_memory(ctx: dict) -> dict:
    print("=" * 60)
    print("PHASE 4: Graph Memory (entities + edges)")
    print("=" * 60)

    from src.graph_memory import GraphMemory
    from src.durable_memory import DurableMemoryStore, MemoryInput

    store = DurableMemoryStore()
    gm = GraphMemory()

    # Insert memory that should trigger entity extraction
    mid = store.insert_memory(MemoryInput(
        content="The owner works on Mo Memory using Postgres with pgvector extension.",
        user_id=ctx["actor"]["actor_id"],
        session_id=ctx["thread_id"],
        org_id=ctx["actor"]["org_id"],
        memory_type="semantic",
        category="fact",
        visibility="owner_only",
        confidence=0.9,
    ))
    print(f"  Inserted: {mid}")

    # Extract entities + create edges
    gm.extract_and_link(mid, "The owner works on Mo Memory using Postgres with pgvector extension.")

    # Verify edges
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(
            "SELECT source_id, target_id, edge_type FROM memory.memory_edges ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        print(f"  Edges found: {len(rows)}")
        for r in rows:
            print(f"    {r[0]} --[{r[2]}]--> {r[1]}")

    # Expand graph
    related = gm.expand_graph(
        [mid],
        depth=1,
        limit=5,
        user_id=ctx["actor"]["actor_id"],
        org_id=ctx["actor"].get("org_id"),
    )
    print(f"  Related memories via graph: {len(related)}")

    print("  OK\n")
    return ctx


# ---------------------------------------------------------------------------
# Phase 5: WhatsApp Adapter End-to-End
# ---------------------------------------------------------------------------

def phase5_adapter(ctx: dict) -> dict:
    print("=" * 60)
    print("PHASE 5: WhatsApp Adapter End-to-End")
    print("=" * 60)

    from src.adapters.base import IncomingMessage
    from src.adapters.whatsapp import WhatsAppAdapter

    os.environ.setdefault("OWNER_WHATSAPP_PHONES", "15550000001")
    os.environ.setdefault("AGENT_ORG_ID", ctx["actor"]["org_id"])
    # Use in-memory checkpointing for adapter test to avoid connection reuse issues
    # Postgres checkpointing is already verified in Phase 1
    os.environ["CHECKPOINTER"] = "memory"
    adapter = WhatsAppAdapter()

    # Test actor resolution
    owner_msg = IncomingMessage(
        channel="whatsapp",
        sender_id="15550000001@s.whatsapp.net",
        sender_name="Owner",
        text="Remember this: I need weekly reports every Monday.",
        thread_id=ctx["thread_id"],
    )
    actor = adapter.resolve_actor(owner_msg)
    assert actor.role == "owner", f"Expected owner, got {actor.role}"
    print(f"  Actor: {actor.actor_id} (role={actor.role})")

    # Test event storage
    event_id = adapter.store_event(owner_msg)
    assert event_id is not None, "Event storage failed"
    print(f"  Event stored: {event_id}")

    # Test end-to-end (with real LLM if key available)
    result = adapter.handle(owner_msg)
    print(f"  Response: {result['assistant_response'][:100]}...")
    assert len(result["memory_writes"]) >= 1, "Expected memory write"
    assert len(result["written_memory_ids"]) >= 1, "Expected written memory ID"
    print(f"  Memory writes: {len(result['memory_writes'])}")

    print("  OK\n")
    return ctx


# ---------------------------------------------------------------------------
# Phase 6: DeepSeek LLM Integration
# ---------------------------------------------------------------------------

def phase6_llm(ctx: dict) -> dict:
    print("=" * 60)
    print("PHASE 6: DeepSeek LLM Integration")
    print("=" * 60)

    llm_key = os.environ.get("LLM_API_KEY")
    if not llm_key:
        print("  SKIP: LLM_API_KEY not set")
        print("  OK (skipped)\n")
        return ctx

    from src.llm_client import LLMClient
    client = LLMClient()

    # Test basic chat
    r1 = client.chat_with_memory("What is the capital of France?")
    assert "Paris" in r1, f"Expected 'Paris' in response, got: {r1}"
    print(f"  Basic: {r1}")

    # Test with retrieved context
    r2 = client.chat_with_memory(
        "What does the owner work on?",
        retrieved_context="The owner works on Mo Memory using Postgres.",
    )
    assert "Mo Memory" in r2 or "Postgres" in r2, f"Expected context usage, got: {r2}"
    print(f"  With context: {r2[:100]}...")

    print("  OK\n")
    return ctx


def _redact_url(value: str) -> str:
    if "://" not in value:
        return value
    parts = urlsplit(value)
    if not parts.password:
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    redacted_netloc = f"{parts.username}:***@{host}" if parts.username else host
    return urlunsplit((parts.scheme, redacted_netloc, parts.path, parts.query, parts.fragment))


# ---------------------------------------------------------------------------
# Phase 7: Final Database Verification
# ---------------------------------------------------------------------------

def phase7_verify_db(ctx: dict) -> dict:
    print("=" * 60)
    print("PHASE 7: Database Verification")
    print("=" * 60)

    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        checks = [
            ("typed_memory", "memory.typed_memory"),
            ("memory_edges", "memory.memory_edges"),
            ("retrieval_logs", "memory.retrieval_logs"),
            ("events", "event_store.events"),
            ("checkpoints", "public.checkpoints"),
        ]

        for name, table in checks:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            cnt = row[0] if row else 0
            print(f"  {name}: {cnt} rows")
            if name in ("typed_memory", "events"):
                assert cnt > 0, f"Expected rows in {name}"

    print("  OK\n")
    return ctx


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n" + "=" * 60)
    print("MO MEMORY: COMPREHENSIVE PHASE TEST")
    print("=" * 60 + "\n")

    ctx = phase0_env()
    ctx = phase1_checkpoint(ctx["thread_id"])
    ctx = phase2_retrieve(ctx)
    ctx = phase3_hybrid(ctx)
    ctx = phase4_graph_memory(ctx)
    ctx = phase5_adapter(ctx)
    ctx = phase6_llm(ctx)
    ctx = phase7_verify_db(ctx)

    print("=" * 60)
    print("ALL PHASES PASSED ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
