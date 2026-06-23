"""Checkpoint configuration for LangGraph.

Phase 2 adds optional Postgres checkpointing. These checkpoint tables are not
product memory tables; they store graph state and thread resumability only.
Durable semantic/episodic/procedural memory remains a later phase.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
import os
from typing import Any

from langgraph.checkpoint.memory import MemorySaver


class CheckpointConfigError(RuntimeError):
    pass


@dataclass
class CheckpointerHandle:
    checkpointer: Any
    mode: str
    context: AbstractContextManager[Any]


def build_memory_checkpointer() -> CheckpointerHandle:
    return CheckpointerHandle(
        checkpointer=MemorySaver(),
        mode="memory",
        context=nullcontext(),
    )


def build_postgres_checkpointer(database_url: str | None = None, *, setup: bool = False) -> CheckpointerHandle:
    database_url = database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise CheckpointConfigError("DATABASE_URL is required for Postgres checkpointing")

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:
        raise CheckpointConfigError(
            "Postgres checkpointing requires langgraph-checkpoint-postgres and psycopg"
        ) from exc

    # Try newer LangGraph API: from_conn_string may return checkpointer directly
    try:
        saver = PostgresSaver.from_conn_string(database_url)
        if hasattr(saver, 'setup'):
            # Newer API: from_conn_string returns PostgresSaver directly
            if setup:
                saver.setup()
            return CheckpointerHandle(
                checkpointer=saver,
                mode="postgres",
                context=nullcontext(),
            )
    except Exception:
        pass
    
    # Older API: from_conn_string returns a context manager
    saver = PostgresSaver.from_conn_string(database_url)
    checkpointer = saver.__enter__()
    if setup:
        checkpointer.setup()
    return CheckpointerHandle(
        checkpointer=checkpointer,
        mode="postgres",
        context=saver,
    )


def build_checkpointer(mode: str | None = None, *, setup: bool = False) -> CheckpointerHandle:
    selected = (mode or os.environ.get("CHECKPOINTER", "memory")).lower()
    if selected in ("memory", "inmemory", "local"):
        return build_memory_checkpointer()
    if selected in ("postgres", "postgresql", "pg"):
        return build_postgres_checkpointer(setup=setup)
    raise CheckpointConfigError(f"Unsupported CHECKPOINTER mode: {selected}")
