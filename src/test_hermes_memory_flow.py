#!/usr/bin/env python3
"""Verify Hermes native memory -> Postgres memory -> graph memory retrieval flow."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
from psycopg.rows import dict_row

from src.graph import build_graph
from src.hermes_native_memory import HermesNativeMemoryStore


def timed(label: str, fn):
    start = perf_counter()
    value = fn()
    elapsed_ms = (perf_counter() - start) * 1000
    print(f"{label}: {elapsed_ms:.1f} ms")
    return value, elapsed_ms


def invoke(graph, *, thread_id: str, actor: dict[str, str], text: str) -> dict:
    return graph.invoke(
        {
            "user_text": text,
            "actor": actor,
            "session_id": thread_id,
            "messages": [],
            "memory_writes": [],
            "written_memory_ids": [],
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def main() -> None:
    os.environ["MEMORY_BACKEND"] = "postgres"
    os.environ["CHECKPOINTER"] = "memory"

    HermesNativeMemoryStore.clear()

    marker = f"hermes-flow-{uuid4().hex[:8]}"
    graph_entity = f"Hermes Flow {marker}"
    actor = {
        "actor_id": f"test_owner_{marker}",
        "org_id": f"test_org_{marker}",
        "role": "owner",
    }
    thread_id = f"thread-{marker}"
    graph = build_graph()

    identity_text = f"I am an AI engineer. Marker {marker}."
    graph_text = f"I am an AI engineer working on {graph_entity} using Postgres."

    first, first_ms = timed(
        "Turn 1 store identity fact",
        lambda: invoke(graph, thread_id=thread_id, actor=actor, text=identity_text),
    )
    assert first["memory_writes"], "Identity fact should be treated as important memory"
    assert first["written_memory_ids"], "Identity fact should be written to Postgres"

    second, second_ms = timed(
        "Turn 2 store graph-linkable fact",
        lambda: invoke(graph, thread_id=thread_id, actor=actor, text=graph_text),
    )
    assert second["memory_writes"], "Graph-linkable fact should be treated as important memory"
    assert second["written_memory_ids"], "Graph-linkable fact should be written to Postgres"

    native_results = HermesNativeMemoryStore().search(
        "what do I do",
        actor_id=actor["actor_id"],
        org_id=actor["org_id"],
        limit=5,
    )
    assert any("AI engineer" in row["content"] for row in native_results), (
        "Hermes native memory should contain the identity fact"
    )

    db_url = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        typed_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM memory.typed_memory
            WHERE user_id = %s AND content ILIKE %s
            """,
            (actor["actor_id"], f"%{marker}%"),
        ).fetchone()["count"]
        assert typed_count >= 2, "Postgres typed memory should contain both facts"

        graph_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM memory.memory_edges
            WHERE metadata->>'memory_id' = %s
            """,
            (second["written_memory_ids"][0],),
        ).fetchone()["count"]
        assert graph_count > 0, "Graph memory should contain edges for the graph-linkable fact"

    third, third_ms = timed(
        "Turn 3 answer from native + Postgres + graph context",
        lambda: invoke(graph, thread_id=thread_id, actor=actor, text="What do I do?"),
    )
    context = third.get("retrieved_context", "")
    assert "[Hermes native]" in context, "Retrieval should check Hermes native memory first"
    assert "[Postgres]" in context, "Retrieval should include durable Postgres memory"
    assert "AI engineer" in context, "Retrieved context should answer the profession question"

    total_ms = first_ms + second_ms + third_ms
    print("\nVerification:")
    print(f"- marker: {marker}")
    print(f"- native records found: {len(native_results)}")
    print(f"- typed_memory rows: {typed_count}")
    print(f"- graph edges for second memory: {graph_count}")
    print(f"- total three-turn measured time: {total_ms:.1f} ms")
    print(f"- final retrieved context:\n{context}")
    print("\nHermes memory flow test passed.")


if __name__ == "__main__":
    main()
