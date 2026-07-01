"""
telemetry — Local skill/command invocation telemetry for the ECC runtime.

Provides:
- ``Event``: an immutable record of one skill/command invocation
- ``EventKind``: enum of event types
- ``EventStore``: protocol that storage backends implement (SQLite, in-memory)
- ``SqliteEventStore``: default persistent store
- ``InMemoryEventStore``: test-friendly store
- ``TelemetryCollector``: thin façade that records events and resolves the
  default store location

The collector is intentionally a *write-only* layer. Reports are produced by
``telemetry.reports``; collection is decoupled from analysis so the writer
can stay fast and side-effect-free for hot-path invocation sites.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

_LOGGER = logging.getLogger("ecc.telemetry")


# Public, exported so callers can refer to these by name without
# importing the strings from elsewhere.
DEFAULT_DB_PATH = Path("~/.ecc/telemetry.db").expanduser()
_INSERT_BATCH_SIZE = 50
_MAX_EVENT_FIELDS_BYTES = 8 * 1024  # cap to keep rows small


class EventKind(str, Enum):  # noqa: UP042 — match existing pattern in src/llm/core/types.py
    """Kind of invocation being recorded.

    The wire format is the lowercase string value (e.g. ``"skill"``)
    so reports can GROUP BY without joining on the enum.
    """

    SKILL = "skill"
    COMMAND = "command"
    AGENT = "agent"


@dataclass(frozen=True)
class Event:
    """One immutable record of a skill/command/agent invocation.

    Frozen so callers cannot mutate history after the fact, and so the
    same event can be safely passed across threads.
    """

    name: str
    kind: EventKind
    started_at: float
    duration_ms: int
    success: bool
    actor_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    def field_dict(self) -> dict[str, object]:
        """Return a dict suitable for SQL parameter binding.

        Keeps the column order stable and trims oversized error
        messages so a single bad invocation cannot bloat the database.
        """
        message = (self.error_message or "")[:_MAX_EVENT_FIELDS_BYTES]
        return {
            "name": self.name,
            "kind": self.kind.value,
            "started_at": self.started_at,
            "duration_ms": max(0, int(self.duration_ms)),
            "success": 1 if self.success else 0,
            "actor_id": self.actor_id,
            "error_type": self.error_type,
            "error_message": message or None,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


class EventStore(Protocol):
    """Storage backend for events.

    Two implementations ship in this package: ``SqliteEventStore`` and
    ``InMemoryEventStore``. Tests can substitute either.
    """

    def insert(self, event: Event) -> None: ...

    def insert_many(self, events: Iterable[Event]) -> int: ...

    def iter_all(self) -> Iterator[Event]: ...

    def close(self) -> None: ...


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    success INTEGER NOT NULL,
    actor_id TEXT,
    error_type TEXT,
    error_message TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER
);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_started_at
    ON telemetry_events(started_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_kind_name
    ON telemetry_events(kind, name, started_at);
-- Partial index for the "show me failing skills" query: most rows
-- are successes so a partial index on the rare failure cases is
-- smaller and faster.
CREATE INDEX IF NOT EXISTS idx_telemetry_events_failures
    ON telemetry_events(kind, name, started_at)
    WHERE success = 0;
"""


# Single source of truth for the column order shared by INSERT, SELECT,
# and the row-to-Event builder. Adding a column means appending it here
# and the rest of the read/write paths stay in sync.
_EVENT_COLUMNS: tuple[str, ...] = (
    "name", "kind", "started_at", "duration_ms", "success",
    "actor_id", "error_type", "error_message", "tokens_in", "tokens_out",
)


def _columns_sql() -> str:
    return ", ".join(_EVENT_COLUMNS)


def _placeholders_sql() -> str:
    return ", ".join(f":{col}" for col in _EVENT_COLUMNS)


_INSERT_SQL = (
    f"INSERT INTO telemetry_events ({_columns_sql()}) "
    f"VALUES ({_placeholders_sql()})"
)


_SELECT_SQL = (
    f"SELECT {_columns_sql()} FROM telemetry_events ORDER BY started_at ASC"
)


class SqliteEventStore:
    """Persistent event store backed by SQLite.

    A single lock guards the connection because SQLite's default
    journal mode does not support concurrent writers from multiple
    Python threads without extra setup. Reads are serialized with
    writes, which is acceptable for a write-heavy telemetry workload.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.executescript(_SCHEMA_SQL)

    def insert(self, event: Event) -> None:
        with self._lock:
            self._conn.execute(_INSERT_SQL, event.field_dict())

    def insert_many(self, events: Iterable[Event]) -> int:
        rows: list[dict[str, object]] = []
        for event in events:
            rows.append(event.field_dict())
        if not rows:
            return 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(_INSERT_SQL, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    def iter_all(self) -> Iterator[Event]:
        with self._lock:
            cursor = self._conn.execute(_SELECT_SQL)
            rows = cursor.fetchall()
        for row in rows:
            yield _row_to_event(row)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_event(row: tuple) -> Event:
    """Build an Event from a SQLite row tuple.

    The row order must match ``_EVENT_COLUMNS`` (the source of truth
    used to generate ``_SELECT_SQL``); the ``_row_to_event`` order
    intentionally uses the same list via destructuring.
    """
    values = dict(zip(_EVENT_COLUMNS, row))
    return Event(
        name=values["name"],
        kind=EventKind(values["kind"]),
        started_at=float(values["started_at"]),
        duration_ms=int(values["duration_ms"]),
        success=bool(values["success"]),
        actor_id=values["actor_id"],
        error_type=values["error_type"],
        error_message=values["error_message"],
        tokens_in=values["tokens_in"],
        tokens_out=values["tokens_out"],
    )


class InMemoryEventStore:
    """Non-persistent event store for tests and ephemeral runs.

    Behaves like ``SqliteEventStore`` for read/write but holds events
    in a list. ``iter_all`` returns events in insertion order.
    """

    def __init__(self) -> None:
        self._events: list[Event] = []

    def insert(self, event: Event) -> None:
        self._events.append(event)

    def insert_many(self, events: Iterable[Event]) -> int:
        before = len(self._events)
        self._events.extend(events)
        return len(self._events) - before

    def iter_all(self) -> Iterator[Event]:
        return iter(list(self._events))

    def close(self) -> None:
        # Nothing to release; method exists for protocol parity.
        return None


@dataclass
class TelemetryCollector:
    """Write-only façade that records events to an ``EventStore``.

    The collector does not perform aggregation. Reports live in
    ``telemetry.reports`` so this class stays small and free of
    dead-weight methods.
    """

    store: EventStore
    _batch: list[Event] = field(default_factory=list, init=False)
    _flush_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def record_invocation(
        self,
        *,
        name: str,
        kind: EventKind,
        duration_ms: int,
        success: bool,
        actor_id: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        """Record one invocation.

        Batched writes (``_batch``) are flushed either when the batch
        reaches ``_INSERT_BATCH_SIZE`` or on ``flush()``. This keeps the
        hot path cheap while ensuring events are durable within one
        batch interval.
        """
        event = Event(
            name=name,
            kind=kind,
            started_at=time.time(),
            duration_ms=duration_ms,
            success=success,
            actor_id=actor_id,
            error_type=error_type,
            error_message=error_message,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        with self._flush_lock:
            self._batch.append(event)
            if len(self._batch) >= _INSERT_BATCH_SIZE:
                self._flush_locked()

    def flush(self) -> None:
        """Force the in-memory batch to the store."""
        with self._flush_lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        # Caller MUST hold ``_flush_lock``.
        if not self._batch:
            return
        batch = self._batch
        self._batch = []
        try:
            self.store.insert_many(batch)
        except Exception as exc:
            # Persistence failure must not break the calling skill.
            # Drop the batch (rather than buffer it forever) so the
            # store does not grow unbounded; log loudly so it shows
            # up in operator dashboards.
            _LOGGER.error("telemetry flush dropped %d event(s): %s",
                          len(batch), exc)


def default_db_path() -> Path:
    """Resolve the default DB path, honoring ``ECC_TELEMETRY_DB``.

    Falls back to a temp file when neither the env var nor the home
    directory is writable, so the collector never raises during
    construction.
    """
    override = os.environ.get("ECC_TELEMETRY_DB")
    if override:
        return Path(override).expanduser()
    try:
        DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return DEFAULT_DB_PATH
    except OSError:
        return Path(tempfile.gettempdir()) / "ecc-telemetry.db"


def open_default_store() -> EventStore:
    """Open the default store based on environment configuration."""
    return SqliteEventStore(default_db_path())


__all__ = (
    "DEFAULT_DB_PATH",
    "Event",
    "EventKind",
    "EventStore",
    "InMemoryEventStore",
    "SqliteEventStore",
    "TelemetryCollector",
    "default_db_path",
    "open_default_store",
)
