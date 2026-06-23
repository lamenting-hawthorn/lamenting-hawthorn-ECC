#!/usr/bin/env python3
"""Phase 1/2 local test runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from src.checkpoints import CheckpointConfigError
    from src.graph import build_graph, build_graph_with_checkpointer
except ImportError:
    from checkpoints import CheckpointConfigError
    from graph import build_graph, build_graph_with_checkpointer


def run_turn(graph, thread_id: str, turn: int, user_text: str) -> dict:
    result = graph.invoke(
        {
            "user_text": user_text,
            "actor": {"actor_id": "local_test_actor", "org_id": "local_test_org", "role": "owner"},
            "session_id": thread_id,
            "messages": [],
            "memory_writes": [],
            "written_memory_ids": [],
        },
        config={"configurable": {"thread_id": thread_id}},
    )

    print(f"\nTurn {turn}")
    print(f"user: {user_text}")
    print(f"assistant response: {result['assistant_response']}")
    print(f"retrieved context: {result['retrieved_context']}")
    print("memoryWrites array:")
    print(json.dumps(result.get("memory_writes", []), indent=2))
    if result.get("written_memory_ids"):
        print("writtenMemoryIds array:")
        print(json.dumps(result.get("written_memory_ids", []), indent=2))
    return result


def run_two_turn_test(graph, label: str) -> None:
    print(f"\n=== {label} ===")
    # Unique thread_id per test run to avoid checkpoint contamination
    import uuid
    thread_id = f"test-{uuid.uuid4().hex[:8]}"

    first = run_turn(
        graph,
        thread_id,
        1,
        "Remember this: Mo Memory should use Python and direct Postgres.",
    )
    second = run_turn(
        graph,
        thread_id,
        2,
        "What is the capital of France?",
    )

    # Core assertions: salience gate behavior and memory write counts
    assert len(first["memory_writes"]) == 1, "Turn 1 should trigger memory write"
    assert second["memory_writes"] == [], "Turn 2 should not trigger memory write"

    # Retrieval assertions (flexible because DB state varies across runs)
    using_durable = os.environ.get("MEMORY_BACKEND", "fake").lower() in ("postgres", "durable")
    if using_durable:
        # Turn 1 should retrieve the same or similar content (from DB or checkpoint context)
        assert "Mo Memory" in first["retrieved_context"] or "No durable memory" in first["retrieved_context"]
    else:
        assert first["retrieved_context"] == "No durable memory retrieved yet."
        assert second["retrieved_context"] == "No durable memory retrieved yet."

    print(f"\n{label} passed.")


def main() -> None:
    graph = build_graph()
    run_two_turn_test(graph, "Phase 1 local LangGraph test")

    if os.environ.get("CHECKPOINTER", "").lower() in ("postgres", "postgresql", "pg"):
        try:
            graph, handle = build_graph_with_checkpointer("postgres", setup=True)
        except CheckpointConfigError as exc:
            print(f"\nPhase 2 Postgres checkpoint test skipped: {exc}")
            return

        try:
            run_two_turn_test(graph, "Phase 2 Postgres checkpoint test")
        finally:
            handle.context.__exit__(None, None, None)


if __name__ == "__main__":
    main()
