#!/usr/bin/env python3
"""Phase 4 graph-to-durable-memory smoke test."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.graph import build_graph


def invoke(graph, thread_id: str, user_text: str) -> dict:
    return graph.invoke(
        {
            "user_text": user_text,
            "actor": {"actor_id": "phase4_actor", "org_id": "phase4_org", "role": "owner"},
            "session_id": thread_id,
            "messages": [],
            "memory_writes": [],
            "written_memory_ids": [],
        },
        config={"configurable": {"thread_id": thread_id}},
    )


def main() -> None:
    os.environ["MEMORY_BACKEND"] = "postgres"
    marker = f"phase4-{uuid4()}"
    graph = build_graph()
    thread_id = f"phase4-{marker}"

    first = invoke(
        graph,
        thread_id,
        f"Remember this: durable graph memory marker {marker}.",
    )
    second = invoke(
        graph,
        thread_id,
        marker,
    )

    print(f"marker: {marker}")
    print(f"written ids: {first.get('written_memory_ids', [])}")
    print(f"retrieved context: {second['retrieved_context']}")

    assert first["written_memory_ids"]
    assert marker in second["retrieved_context"]
    assert second["memory_writes"] == []

    print("\nPhase 4 graph durable memory test passed.")


if __name__ == "__main__":
    main()
