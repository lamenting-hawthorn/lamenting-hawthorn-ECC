#!/usr/bin/env python3
"""Phase 2 Postgres checkpoint smoke test.

Requires:
  CHECKPOINTER=postgres
  DATABASE_URL=postgresql://...

This tests LangGraph checkpoint persistence only. It does not write durable
product memory.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.checkpoints import CheckpointConfigError
from src.graph import build_graph_with_checkpointer
from src.test import run_two_turn_test


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is required")

    try:
        graph, handle = build_graph_with_checkpointer("postgres", setup=True)
    except CheckpointConfigError as exc:
        raise SystemExit(f"Could not build Postgres checkpointer: {exc}") from exc

    try:
        run_two_turn_test(graph, "Phase 2 Postgres checkpoint test")
    finally:
        handle.context.__exit__(None, None, None)


if __name__ == "__main__":
    main()
