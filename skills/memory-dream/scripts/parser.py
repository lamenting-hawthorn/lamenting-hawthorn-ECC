"""
parser.py — Read ``memory.typed_memory`` rows into structured entries
for the dream curation pipeline.

Turns a memory "store" into
structured records the curator can reason about and the diff report
can summarize. Here the "store" is the ``memory.typed_memory`` table
instead of a §-delimited markdown file.

One MemoryEntry corresponds to one row. ``source`` is the row's
``source`` column (e.g. ``user_utterance``, ``hermes_import``) and the
``index`` is the row's position in the result set after ordering.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass
class MemoryEntry:
    """One typed_memory row, normalized for the dream pipeline."""

    row_id: str
    text: str
    memory_type: str
    category: str
    confidence: float
    source: str
    visibility: str
    user_id: str
    created_at: str
    index: int
    hash: str = ""
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = _hash(self.text)


@dataclass
class ParsedStore:
    """Result of reading typed_memory for one actor scope."""

    user_id: str
    entries: list[MemoryEntry]
    char_count: int

    @property
    def entry_count(self) -> int:
        return len(self.entries)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).lower().encode()).hexdigest()[:12]


DEFAULT_DATABASE_URL = "postgresql:///agent_memory"

# Default sort: episodic by recency, semantic by confidence then recency,
# procedural by recency. Same row may appear in only one section; we
# order globally for stable index assignment.
_DEFAULT_ORDER_SQL = """
    order by
        case memory_type
            when 'episodic'   then 0
            when 'semantic'   then 1
            when 'procedural' then 2
            else 3
        end,
        case memory_type
            when 'semantic' then confidence
            else 0
        end desc,
        created_at desc
"""


def _connect(database_url: str | None = None):
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(url, row_factory=dict_row)


def parse_typed_memory(
    *,
    user_id: str,
    org_id: str | None = None,
    memory_types: Sequence[str] | None = None,
    skip_superseded: bool = True,
    database_url: str | None = None,
) -> ParsedStore:
    """
    Read all (non-superseded) typed_memory rows for a user into a
    :class:`ParsedStore`.

    Args:
        user_id:    whose memory to read. Required.
        org_id:     optional org scope. When set, rows whose ``org_id``
                    matches are also included (so team-scoped rows surface
                    in the curator pass).
        memory_types:  optional subset (``['semantic', 'procedural']``).
                    Defaults to all three types.
        skip_superseded:  if True (default), exclude rows where
                    ``superseded_by IS NOT NULL``. The curator should
                    not propose to re-merge already-superseded rows.
        database_url:  optional override; defaults to ``$DATABASE_URL``.
    """
    where = [
        "user_id = %s",
        "(%s::text IS NULL OR org_id = %s::text OR visibility = 'owner_only')",
    ]
    params: list = [user_id, org_id, org_id]

    if memory_types:
        where.append("memory_type = ANY(%s)")
        params.append(list(memory_types))

    if skip_superseded:
        where.append("superseded_by IS NULL")

    # Safe: only literal fragments from `where` and the constant
    # _DEFAULT_ORDER_SQL are interpolated. All user values go through
    # `%s` placeholders (see `params`). If anyone appends a runtime
    # value to `where` in the future, this becomes a SQL-injection
    # vector — keep that in mind when editing.
    sql = f"""
        select
            id, memory_type, category, content, summary,
            confidence, source, visibility, user_id, session_id,
            org_id, created_at, superseded_by
        from memory.typed_memory
        where {" and ".join(where)}
        {_DEFAULT_ORDER_SQL}
    """  # noqa: S608

    entries: list[MemoryEntry] = []
    total_chars = 0
    with _connect(database_url) as conn:
        for i, row in enumerate(conn.execute(sql, params).fetchall()):
            text = (row.get("summary") or row.get("content") or "").strip()
            if not text:
                continue
            entry = MemoryEntry(
                row_id=str(row["id"]),
                text=text,
                memory_type=row["memory_type"],
                category=row["category"],
                confidence=float(row["confidence"] or 0.0),
                source=row["source"],
                visibility=row["visibility"],
                user_id=row["user_id"],
                created_at=row["created_at"].isoformat()
                if row.get("created_at") else "",
                index=i,
                superseded_by=str(row["superseded_by"]) if row.get("superseded_by") else None,
            )
            entries.append(entry)
            total_chars += len(text)

    return ParsedStore(user_id=user_id, entries=entries, char_count=total_chars)


def render_entries(entries: Iterable[MemoryEntry]) -> str:
    """Serialize entries back to a §-delimited string for diff display.

    The output is informational — adopt() does not actually re-write
    this format; it writes back to typed_memory with proper schema
    fields. This helper exists so diff.md and report.md are readable
    in the same human-readable format used by the diff report.
    """
    return "\n§\n".join(_format_entry(e) for e in entries)


def _format_entry(e: MemoryEntry) -> str:
    return (
        f"[{e.memory_type}/{e.category} c={e.confidence:.2f}] {e.text}"
    )


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ACTOR_ID", "u_owner")
    store = parse_typed_memory(user_id=target)
    print(f"user_id={store.user_id}  entries={store.entry_count}  chars={store.char_count}")
    for e in store.entries[:10]:
        print(f"  [{e.hash}] ({e.memory_type}/{e.category}) {e.text[:80]}{'…' if len(e.text) > 80 else ''}")
    if store.entry_count > 10:
        print(f"  …and {store.entry_count - 10} more")
