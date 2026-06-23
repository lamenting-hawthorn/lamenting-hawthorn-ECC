#!/usr/bin/env python3
"""Test the TTL cleanup mechanism.

1. Insert a row with ``expires_at = NOW() + interval '1 second'``.
2. Sleep 2.3 seconds so the row is past its TTL.
3. Call the cleanup sweep and verify the row is gone.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so ``import src`` works.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This import auto-starts the background daemon thread, but our test uses
# the explicit cleanup_once() method for deterministic control.
from src import TtlCleaner

try:
    import psycopg
except ImportError:
    print("SKIP: psycopg is not installed")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")


def _postgres_available() -> bool:
    try:
        with psycopg.connect(DATABASE_URL):
            return True
    except Exception:
        return False


def _row_exists(conn, memory_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM memory.typed_memory WHERE id = %s", (memory_id,)
    ).fetchone()
    return row is not None


def test_ttl_cleanup() -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available")
    # 1 — Connect and insert a short-lived row
    with psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("SELECT set_config('app.current_role', 'service', false)")
        row = conn.execute(
            """
            INSERT INTO memory.typed_memory
                (memory_type, category, content, user_id, session_id,
                 org_id, role, expires_at)
            VALUES
                ('semantic', 'fact', 'expires-soon', 'ttl_tester',
                 'ttl-test-session', 'ttl_org', 'owner',
                 now() + interval '1 second')
            RETURNING id
            """
        ).fetchone()
        memory_id = str(row["id"])
        assert _row_exists(conn, memory_id), "row should exist right after insert"
        print(f"  inserted row {memory_id} with expires_at = NOW()+1s")

    print("  sleeping 2.3s for TTL to pass …")
    time.sleep(2.3)

    # 2 — Run the cleanup sweep explicitly
    cleaner = TtlCleaner(database_url=DATABASE_URL, interval_seconds=10.0)
    deleted = cleaner.cleanup_once()
    print(f"  cleanup_once() deleted {deleted} row(s)")

    # 3 — Verify the row is gone
    with psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("SELECT set_config('app.current_role', 'service', false)")
        if _row_exists(conn, memory_id):
            print("FAIL: expired row still exists — TTL cleanup did not delete it")
            sys.exit(1)

    print("PASS: expired row was deleted by TTL cleanup")


if __name__ == "__main__":
    # Quick connectivity check before running
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        print(f"SKIP: cannot connect to database ({exc})")
        sys.exit(0)

    print("=== TTL Cleanup Test ===")
    test_ttl_cleanup()
    print("Done.")
