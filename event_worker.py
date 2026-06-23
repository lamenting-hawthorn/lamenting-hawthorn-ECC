#!/usr/bin/env python3
"""
event_worker.py — Layer 1 → Layer 2 Background Drain Worker.
=============================================================

Drains raw events from event_store.events through the salience gate,
classifier, and embedder into memory.typed_memory.

Usage:
    # Run once (process all pending events and exit)
    python event_worker.py --once

    # Continuous mode (poll every 5 seconds)
    python event_worker.py --interval 5

    # Daemon mode with systemd / supervisor
    python event_worker.py

Design:
    - Polls for unprocessed events (processed_at IS NULL)
    - Applies salience_gate() — drops trivial, duplicate, noisy events
    - Classifies events into memory_type + category
    - Generates embeddings for semantic/procedural
    - Writes to typed_memory
    - Creates graph edges to related memories
    - Writes audit log entries
    - Marks events as processed
    - FOR UPDATE SKIP LOCKED ensures multiple workers don't conflict
"""

import argparse
import asyncio
import json
import os
import sys
import signal
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    import asyncpg
except ImportError:
    print("Install asyncpg: pip install asyncpg")
    sys.exit(1)

try:
    import httpx
except ImportError:
    print("Install httpx: pip install httpx")
    sys.exit(1)


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql:///agent_memory",
)
BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", "50"))
POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", "5"))
EMBEDDING_API_URL = os.environ.get(
    "EMBEDDING_API_URL",
    "https://api.openai.com/v1/embeddings",
)
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")

# Logging
VERBOSE = os.environ.get("WORKER_VERBOSE", "0") == "1"


# ---------------------------------------------------------------------------
# SALIENCE GATE
# ---------------------------------------------------------------------------

TRIVIAL_PATTERNS = {"lol", "ok", "thanks", "👍", "k", "yes", "no",
                    "okay", "sure", "cool", "nice", "got it", "k."}

NOISE_EVENT_TYPES = {"system_heartbeat", "connection_ping", "health_check"}


def salience_gate(event: Dict) -> Tuple[bool, Optional[str]]:
    """
    Returns (passes_gate: bool, reason_for_rejection: Optional[str]).

    An event passes the gate if:
      - It has meaningful content (text >= 5 chars OR has tool calls)
      - It is not a trivial response
      - It is not system noise
      - It is not a duplicate (idempotency check is done separately)
      - The user has write permission
    """
    # Rule 1: system noise
    if event.get("event_type") in NOISE_EVENT_TYPES:
        return False, "system_noise"

    # Rule 2: minimum content
    payload = event.get("payload", {}) or {}
    text = (payload.get("text") or "").strip()
    has_tool_calls = bool(payload.get("tool_calls"))

    if len(text) < 5 and not has_tool_calls:
        return False, "too_short"

    # Rule 3: trivial exchanges
    if text.lower().strip(".!?") in TRIVIAL_PATTERNS:
        return False, "trivial"

    # Rule 4: permission check
    role = (event.get("role") or "user").lower()
    if role not in ("owner", "admin", "team", "user"):
        return False, "permission_denied"

    return True, None


# ---------------------------------------------------------------------------
# CLASSIFIER
# ---------------------------------------------------------------------------

PREFERENCE_TRIGGERS = {"i prefer", "i like", "i don't like", "my favorite",
                       "please use", "always use", "never use", "i want"}
FACT_TRIGGERS = {"is called", "runs on", "uses", "hosted on", "costs",
                 "version", "located at", "url is", "email is"}
CORRECTION_TRIGGERS = {"no, i meant", "that's wrong", "actually", "correct that",
                       "i meant", "not that", "instead of"}


def classify_event(event: Dict) -> Tuple[str, str, float]:
    """
    Classify a raw event into (memory_type, category, confidence).

    Uses rule-based heuristic first, then falls back to LLM classification
    for ambiguous cases.
    """
    payload = event.get("payload", {}) or {}
    text = (payload.get("text") or "").lower().strip()
    event_type = event.get("event_type", "")

    # User messages
    if event_type == "message_received":
        # Correction patterns — highest priority
        if any(p in text for p in CORRECTION_TRIGGERS):
            return ("semantic", "correction", 1.0)

        # Explicitly stated preferences
        if any(p in text for p in PREFERENCE_TRIGGERS):
            return ("semantic", "preference", 0.8)

        # Factual statements
        if any(p in text for p in FACT_TRIGGERS):
            return ("semantic", "fact", 0.7)

        # Default: conversation context (episodic)
        return ("episodic", "interaction", 0.5)

    # Tool calls suggest procedural knowledge
    if event_type == "tool_call" or payload.get("tool_calls"):
        return ("procedural", "procedure", 0.6)

    # System events (agent inferences, errors)
    if event_type == "agent_inference":
        return ("semantic", "fact", 0.5)

    # Fallback: episodic
    return ("episodic", "interaction", 0.3)


def resolve_visibility(role: str) -> str:
    role = (role or "user").lower()
    if role == "owner":
        return "owner_only"
    return "team"


def calculate_expiry(memory_type: str) -> Optional[str]:
    if memory_type == "episodic":
        dt = datetime.now(timezone.utc) + timedelta(days=30)
        return dt.isoformat()
    if memory_type in ("semantic", "procedural"):
        return None  # permanent
    return None


def map_event_source(event: Dict) -> str:
    event_type = event.get("event_type", "")
    if event_type == "message_received":
        return "user_utterance"
    if event_type == "tool_call":
        return "tool_result"
    if event_type == "agent_inference":
        return "agent_inference"
    if event_type == "knowledge_base_import":
        return "knowledge_base_import"
    return "agent_inference"


def redact_db_url(db_url: str) -> str:
    """Hide credentials before printing connection strings."""
    if "://" not in db_url or "@" not in db_url:
        return db_url
    scheme, rest = db_url.split("://", 1)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://***:***@{host}"


def encode_pgvector(embedding: Optional[List[float]]) -> Optional[str]:
    """asyncpg needs pgvector values encoded unless a pgvector codec is registered."""
    if embedding is None:
        return None
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


# ---------------------------------------------------------------------------
# EMBEDDING CLIENT
# ---------------------------------------------------------------------------

class EmbeddingClient:
    """Simple embedding client with retries."""

    def __init__(self, api_url: str, api_key: str, model: str):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self._client.aclose()

    async def embed(self, text: str) -> Optional[List[float]]:
        if not self.api_key and "openai" in self.api_url.lower():
            if VERBOSE:
                print("  [embed] No API key set, skipping embedding")
            return None

        for attempt in range(3):
            try:
                resp = await self._client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "input": text[:8000],
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                if "data" in data and len(data["data"]) > 0:
                    return data["data"][0]["embedding"]
                if isinstance(data, list) and len(data) > 0:
                    return data[0] if isinstance(data[0], list) else data[0].get("embedding")

                print(f"  [embed] Unexpected response format: {type(data)}")
                return None

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    # Rate limited — wait and retry
                    wait = 2 ** attempt
                    if VERBOSE:
                        print(f"  [embed] Rate limited, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                if VERBOSE:
                    print(f"  [embed] HTTP {e.response.status_code}: {e.response.text[:200]}")
                return None

            except httpx.TimeoutException:
                if VERBOSE:
                    print(f"  [embed] Timeout (attempt {attempt + 1}/3)")
                await asyncio.sleep(1)
                continue

            except Exception as e:
                if VERBOSE:
                    print(f"  [embed] Error: {e}")
                return None

        return None


# ---------------------------------------------------------------------------
# WORKER
# ---------------------------------------------------------------------------

class EventWorker:
    """Drains events from event_store.events → memory.typed_memory."""

    def __init__(self, db_url: str, batch_size: int = BATCH_SIZE,
                 poll_interval: int = POLL_INTERVAL, once: bool = False):
        self.db_url = db_url
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.once = once
        self._db = None
        self._embedder = EmbeddingClient(
            EMBEDDING_API_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL
        )
        self._running = True
        self.stats = {
            "events_processed": 0,
            "memories_written": 0,
            "edges_created": 0,
            "dropped_too_short": 0,
            "dropped_trivial": 0,
            "dropped_noise": 0,
            "dropped_duplicate": 0,
            "dropped_permission": 0,
            "errors": 0,
        }

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, sig, frame):
        print("\n[worker] Shutting down...")
        self._running = False

    async def connect(self):
        self._db = await asyncpg.connect(self.db_url)
        await self._db.execute("SELECT set_config('app.current_role', 'service', false)")

    async def close(self):
        if self._db:
            await self._db.close()
        await self._embedder.close()

    async def fetch_unprocessed(self) -> List[Dict]:
        """Atomically claim the next batch of unprocessed events."""
        rows = await self._db.fetch(
            """
            WITH next_events AS (
                SELECT id, created_at
                FROM event_store.events
                WHERE processed_at IS NULL
                  AND (
                      processing_started_at IS NULL
                      OR processing_started_at < now() - interval '15 minutes'
                  )
                  AND processing_attempts < 5
                ORDER BY created_at ASC
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE event_store.events e
            SET processing_started_at = now(),
                processing_attempts = processing_attempts + 1,
                processing_error = NULL
            FROM next_events n
            WHERE e.id = n.id
              AND e.created_at = n.created_at
            RETURNING e.*
            """,
            self.batch_size,
        )
        return [dict(r) for r in rows]

    async def find_related_memories(
        self, memory_type: str, category: str, content: str, user_id: str
    ) -> List[Dict]:
        """Find existing typed_memory that relates to this new memory."""
        # Only find relationships for semantic memory (non-trivial)
        if memory_type != "semantic":
            return []

        # Check for existing facts or preferences from the same user
        related = await self._db.fetch(
            """
            SELECT id, content, memory_type, category
            FROM memory.typed_memory
            WHERE user_id = $1
              AND memory_type = 'semantic'
              AND category IN ('fact', 'preference')
              AND confidence >= 0.7
              AND to_tsvector('english', content)
                  @@ plainto_tsquery('english', $2)
            LIMIT 5
            """,
            user_id, content,
        )
        return [dict(r) for r in related]

    async def process_event(self, event: Dict) -> None:
        """Process a single event through the pipeline."""
        event_id = event["id"]

        # 1. Salience gate
        passes, reason = salience_gate(event)
        if not passes:
            if reason == "too_short":
                self.stats["dropped_too_short"] += 1
            elif reason == "trivial":
                self.stats["dropped_trivial"] += 1
            elif reason == "permission_denied":
                self.stats["dropped_permission"] += 1
            else:
                self.stats["dropped_noise"] += 1
            await self._mark_processed(event_id)
            return

        # 2. Check idempotency (source_event_id should be unique)
        duplicate = await self._db.fetchval(
            """
            SELECT id FROM memory.typed_memory
            WHERE source_event_id = $1
            LIMIT 1
            """,
            event_id,
        )
        if duplicate:
            self.stats["dropped_duplicate"] += 1
            await self._mark_processed(event_id)
            return

        # 3. Classify
        memory_type, category, confidence = classify_event(event)
        content = (event.get("payload", {}) or {}).get("text", "") or ""

        if not content:
            # No text to store — mark processed but don't write memory
            await self._mark_processed(event_id)
            return

        # 4. Generate embedding (semantic/procedural only)
        embedding = None
        if memory_type in ("semantic", "procedural"):
            embedding = await self._embedder.embed(content)

        # 5. Write to typed_memory
        visibility = resolve_visibility(event.get("role", "user"))
        source = map_event_source(event)

        memory_id = await self._db.fetchval(
            """
            INSERT INTO memory.typed_memory
                (memory_type, category, content, user_id, session_id, org_id,
                 role, visibility, confidence, source, embedding, metadata,
                 expires_at, source_event_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING
            RETURNING id
            """,
            memory_type, category, content,
            event["user_id"], event["session_id"], event.get("org_id"),
            event.get("role", "user"), visibility, confidence, source,
            encode_pgvector(embedding),
            json.dumps({
                "event_type": event["event_type"],
                "source": event.get("source", ""),
                "idempotency_key": event.get("idempotency_key"),
            }),
            calculate_expiry(memory_type),
            event_id,
        )
        if memory_id is None:
            self.stats["dropped_duplicate"] += 1
            await self._mark_processed(event_id)
            return
        self.stats["memories_written"] += 1

        # 6. Create graph edges to related memories
        related = await self.find_related_memories(
            memory_type, category, content, event["user_id"]
        )
        for rel in related:
            try:
                await self._db.execute(
                    """
                    INSERT INTO memory.memory_edges
                        (source_id, target_id, edge_type, weight, created_by, metadata)
                    VALUES ($1, $2, 'related_to', 0.5, 'system', $3)
                    ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                    """,
                    memory_id, rel["id"],
                    json.dumps({"triggered_by_event": str(event_id)}),
                )
                self.stats["edges_created"] += 1
            except Exception as e:
                if VERBOSE:
                    print(f"  [warn] Edge creation: {e}")

        # 7. Audit log
        try:
            await self._db.execute(
                """
                INSERT INTO memory.audit_log
                    (event_type, user_id, session_id, target_id, details)
                VALUES ('memory_written', $1, $2, $3, $4)
                """,
                event["user_id"], event["session_id"], memory_id,
                json.dumps({
                    "memory_type": memory_type,
                    "category": category,
                    "confidence": confidence,
                    "source": source,
                    "event_id": str(event_id),
                }),
            )
        except Exception as e:
            if VERBOSE:
                print(f"  [warn] Audit log: {e}")

        # 8. Mark event as processed
        await self._mark_processed(event_id)

    async def _mark_processed(self, event_id):
        await self._db.execute(
            """
            UPDATE event_store.events
            SET processed_at = now(),
                processing_started_at = NULL,
                processing_error = NULL
            WHERE id = $1
            """,
            event_id,
        )

    async def _mark_failed(self, event_id, error: Exception):
        await self._db.execute(
            """
            UPDATE event_store.events
            SET processing_started_at = NULL,
                processing_error = $2
            WHERE id = $1
            """,
            event_id,
            str(error)[:1000],
        )

    async def run_once(self):
        """Process a single batch and return."""
        batch = await self.fetch_unprocessed()
        if not batch:
            print("[worker] No unprocessed events.")
            return

        print(f"[worker] Processing {len(batch)} events...")
        for event in batch:
            try:
                await self.process_event(event)
                self.stats["events_processed"] += 1
            except Exception as e:
                print(f"[worker] Error processing event {event['id']}: {e}")
                self.stats["errors"] += 1
                try:
                    await self._mark_failed(event["id"], e)
                except Exception:
                    pass

    async def run_loop(self):
        """Continuous polling loop."""
        print(f"[worker] Starting event drain worker "
              f"(batch={self.batch_size}, interval={self.poll_interval}s)")
        print(f"[worker] DB: {redact_db_url(self.db_url)}")
        print(f"[worker] Embedding: {EMBEDDING_MODEL} @ "
              f"{EMBEDDING_API_URL if EMBEDDING_API_KEY else '(disabled)'}")

        while self._running:
            try:
                batch = await self.fetch_unprocessed()
                if not batch:
                    await asyncio.sleep(self.poll_interval)
                    continue

                t0 = datetime.now(timezone.utc)
                print(f"[worker] Processing {len(batch)} events...")

                for event in batch:
                    if not self._running:
                        break
                    try:
                        await self.process_event(event)
                        self.stats["events_processed"] += 1
                    except Exception as e:
                        print(f"[worker] Error processing event {event['id']}: {e}")
                        self.stats["errors"] += 1
                        try:
                            await self._mark_failed(event["id"], e)
                        except Exception:
                            pass

                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                print(f"[worker] Done ({elapsed:.1f}s). "
                      f"Total: {self.stats['events_processed']} events → "
                      f"{self.stats['memories_written']} memories, "
                      f"{self.stats['edges_created']} edges, "
                      f"{self.stats['errors']} errors. "
                      f"Dropped: {self.stats['dropped_trivial']} trivial, "
                      f"{self.stats['dropped_too_short']} short, "
                      f"{self.stats['dropped_noise']} noise")

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[worker] Fatal error in poll loop: {e}")
                if self._running:
                    await asyncio.sleep(self.poll_interval * 2)

        await self.close()
        print("[worker] Stopped.")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Event drain worker: Layer 1 → Layer 2",
    )
    parser.add_argument("--once", action="store_true",
                        help="Process all pending events and exit")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    parser.add_argument("--batch", type=int, default=BATCH_SIZE,
                        help=f"Batch size (default: {BATCH_SIZE})")
    parser.add_argument("--db-url", default=DATABASE_URL,
                        help="Postgres connection string")
    args = parser.parse_args()

    worker = EventWorker(
        db_url=args.db_url,
        batch_size=args.batch,
        poll_interval=args.interval,
        once=args.once,
    )

    try:
        await worker.connect()

        if args.once:
            await worker.run_once()
        else:
            await worker.run_loop()

    except KeyboardInterrupt:
        print("\n[worker] Interrupted.")
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())

# =================================================================
# systemd service file (save as /etc/systemd/system/agent-event-worker.service)
# =================================================================
# [Unit]
# Description=Agent Memory Event Worker (Layer 1 → Layer 2)
# After=network.target postgresql.service
#
# [Service]
# Type=simple
# User=agent-memory
# WorkingDirectory=/opt/agent-memory
# EnvironmentFile=/opt/agent-memory/.env
# Environment=WORKER_VERBOSE=0
# ExecStart=/usr/bin/python3 /opt/agent-memory/event_worker.py
# Restart=always
# RestartSec=10
#
# [Install]
# WantedBy=multi-user.target
