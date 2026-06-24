#!/usr/bin/env python3
"""Drain pending events from event_store.events into memory.typed_memory.

This script wraps event_worker.py and runs it once, printing before/after
row counts for typed_memory.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
from psycopg.rows import dict_row


def _typed_memory_count() -> int:
    url = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM memory.typed_memory").fetchone()
        return row["cnt"] if row else 0


def _unprocessed_events_count() -> int:
    url = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM event_store.events WHERE processed_at IS NULL"
        ).fetchone()
        return row["cnt"] if row else 0


async def main() -> int:
    before = _typed_memory_count()
    events_before = _unprocessed_events_count()
    print(f"[drain] typed_memory rows before: {before}")
    print(f"[drain] unprocessed events before: {events_before}")

    # Import and run the event worker once
    try:
        from event_worker import EventWorker
    except ImportError as exc:
        print(f"[drain] ERROR: cannot import event_worker: {exc}")
        return 1

    db_url = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
    worker = EventWorker(db_url=db_url, once=True)

    try:
        await worker.connect()
        # Drain all pending events — keep running while there are batches
        while True:
            batch = await worker.fetch_unprocessed()
            if not batch:
                break
            print(f"[drain] Processing batch of {len(batch)} events...")
            for event in batch:
                try:
                    await worker.process_event(event)
                    worker.stats["events_processed"] += 1
                except Exception as e:
                    print(f"[drain] Error processing event {event['id']}: {e}")
                    worker.stats["errors"] += 1
                    try:
                        await worker._mark_failed(event["id"], e)
                    except Exception:
                        pass
    except Exception as exc:
        print(f"[drain] ERROR: {exc}")
        return 1
    finally:
        await worker.close()

    after = _typed_memory_count()
    events_after = _unprocessed_events_count()
    print(f"[drain] typed_memory rows after: {after}")
    print(f"[drain] unprocessed events after: {events_after}")
    print(f"[drain] events processed this run: {worker.stats['events_processed']}")
    print(f"[drain] memories written this run: {worker.stats['memories_written']}")
    print(f"[drain] edges created this run: {worker.stats['edges_created']}")
    print(f"[drain] dropped (trivial/short/noise/perm/duplicate): "
          f"{worker.stats['dropped_trivial']}/{worker.stats['dropped_too_short']}/"
          f"{worker.stats['dropped_noise']}/{worker.stats['dropped_permission']}/"
          f"{worker.stats['dropped_duplicate']}")
    print(f"[drain] errors: {worker.stats['errors']}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
