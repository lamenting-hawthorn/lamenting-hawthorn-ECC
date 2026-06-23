"""Base adapter interface for messaging channels.

All channel adapters (WhatsApp, Telegram, CLI) implement this interface.
The adapter's job is:
1. Receive/normalize an incoming message
2. Resolve sender identity to actor
3. Call the LangGraph workflow
4. Return the assistant response
5. Optionally store the raw event
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

DEFAULT_DATABASE_URL = "postgresql:///agent_memory"


@dataclass(frozen=True)
class IncomingMessage:
    channel: str           # 'whatsapp' | 'telegram' | 'cli'
    sender_id: str         # phone number, telegram ID, etc.
    sender_name: str       # display name if available
    text: str
    thread_id: str         # conversation identifier (chat ID, group ID)
    timestamp: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ActorIdentity:
    actor_id: str
    org_id: str
    role: str  # 'owner' | 'team' | 'reader'


def resolve_runtime_org_id() -> str:
    org_id = os.environ.get("AGENT_ORG_ID", "").strip()
    if not org_id:
        raise ValueError("AGENT_ORG_ID is required for adapter identity resolution")
    return org_id


class BaseAdapter(ABC):
    """Abstract base for all messaging adapters."""

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

    def _connect(self):
        conn = psycopg.connect(self.database_url, row_factory=dict_row)
        conn.execute("SELECT set_config('app.current_role', 'service', false)")
        return conn

    @abstractmethod
    def resolve_actor(self, msg: IncomingMessage) -> ActorIdentity:
        """Map sender to actor identity."""
        ...

    def store_event(self, msg: IncomingMessage) -> str | None:
        """Store raw incoming message in event_store.events."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO event_store.events
                        (event_type, source, user_id, session_id, payload, metadata)
                    VALUES
                        ('message_received', %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        msg.channel,
                        msg.sender_id,
                        msg.thread_id,
                        psycopg.types.json.Jsonb({
                            "text": msg.text,
                            "sender_name": msg.sender_name,
                        }),
                        psycopg.types.json.Jsonb(msg.metadata or {}),
                    ),
                ).fetchone()
                return str(row["id"]) if row else None
        except Exception:
            return None  # Non-blocking; logging failure should not break the flow

    def handle(self, msg: IncomingMessage) -> dict[str, Any]:
        """Main entry: resolve → store → invoke graph → return."""
        actor = self.resolve_actor(msg)
        event_id = self.store_event(msg)

        # Build graph state
        state = {
            "user_text": msg.text,
            "actor": {
                "actor_id": actor.actor_id,
                "org_id": actor.org_id,
                "role": actor.role,
            },
            "session_id": msg.thread_id,
            "messages": [],
            "memory_writes": [],
            "written_memory_ids": [],
        }

        # Import graph lazily to avoid circular deps
        try:
            from src.graph import build_graph
            from src.checkpoints import build_checkpointer
        except ImportError:
            from graph import build_graph
            from checkpoints import build_checkpointer

        checkpointer = build_checkpointer().checkpointer
        graph = build_graph(checkpointer)

        result = graph.invoke(
            state,
            config={"configurable": {"thread_id": msg.thread_id}},
        )

        return {
            "actor": actor,
            "event_id": event_id,
            "assistant_response": result.get("assistant_response", ""),
            "retrieved_context": result.get("retrieved_context", ""),
            "memory_writes": result.get("memory_writes", []),
            "written_memory_ids": result.get("written_memory_ids", []),
        }
