#!/usr/bin/env python3
"""Phase 3 durable memory smoke test."""

from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.durable_memory import DurableMemoryStore, MemoryInput


def main() -> None:
    store = DurableMemoryStore()
    marker = f"phase3-{uuid4()}"
    content = f"Mo Memory durable product memory uses local Postgres typed_memory marker {marker}."

    memory_id = store.insert_memory(
        MemoryInput(
            content=content,
            user_id="phase3_test_actor",
            session_id="phase3_test_session",
            org_id="phase3_test_org",
            summary="Mo Memory durable product memory stack",
            metadata={"phase": 3, "test": "durable_memory"},
        )
    )

    results = store.search_memory_basic(
        marker,
        user_id="phase3_test_actor",
        session_id="phase3_test_session",
        org_id="phase3_test_org",
    )

    print(f"inserted memory id: {memory_id}")
    print(f"search results: {len(results)}")
    for row in results:
        print(f"- {row['category']} {row['confidence']}: {row['content']}")

    assert memory_id
    assert any(str(row["id"]) == memory_id for row in results)

    print("\nPhase 3 durable memory test passed.")


if __name__ == "__main__":
    main()
