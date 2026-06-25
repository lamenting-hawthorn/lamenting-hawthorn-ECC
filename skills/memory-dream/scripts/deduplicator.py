"""
deduplicator.py — Find duplicate and near-duplicate typed_memory rows.

In hermes-dream the dedup is hash-based (case/whitespace normalized
SHA-256) plus substring containment plus common-prefix detection,
because the input is plain text. Here, the input is ``typed_memory``
rows that already carry a ``vector(1536)`` embedding, so we can do
*real* semantic dedup:

  - **exact**     — identical (case+whitespace normalized) text.
  - **substring** — one row's text is contained in another's.
  - **prefix**    — long common prefix (likely the same fact edited).
  - **semantic**  — pgvector cosine similarity >= threshold (default
                   ``0.92``) over the same memory_type + category.

Semantic dedup here is the easy part — the runtime already produced
the embedding. The hard part (paraphrased duplicates with low
lexical overlap) is what the LLM pass catches in synthesis.

Every dedup pass is permission-filtered to the same actor scope
passed to :func:`parser.parse_typed_memory`. The dream never proposes
to merge rows owned by different actors.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
from parser import MemoryEntry, ParsedStore
from psycopg.rows import dict_row

DEFAULT_DATABASE_URL = "postgresql:///agent_memory"

# Default cosine similarity threshold. Tuned for all-MiniLM-L6-v2 +
# zero-padded-to-1536. Real duplicates (rephrasings of the same fact)
# land at 0.90-0.96; near-paraphrases of related-but-distinct facts
# land at 0.80-0.88. We pick 0.92 to lean conservative — the LLM
# pass will catch the rest.
DEFAULT_SEMANTIC_THRESHOLD = 0.92

# How many candidates the vector index returns. 50 is a comfortable
# default for a 500-row store; bump for larger stores via flag.
DEFAULT_VECTOR_CANDIDATES = 50


@dataclass
class DuplicateGroup:
    """A cluster of typed_memory rows that look like duplicates."""

    canonical: MemoryEntry
    members: list[MemoryEntry]
    reason: str  # "exact" | "substring" | "prefix" | "semantic"
    similarity: float = 1.0  # for "semantic", the cosine score; 1.0 otherwise

    def __repr__(self) -> str:
        return (
            f"DuplicateGroup({self.reason}, {len(self.members)} entries, "
            f"sim={self.similarity:.3f})"
        )


def find_exact_dupes(entries: list[MemoryEntry]) -> list[DuplicateGroup]:
    """Group entries whose text is identical after case+whitespace normalization."""
    groups: dict[str, list[MemoryEntry]] = {}
    for e in entries:
        groups.setdefault(e.hash, []).append(e)

    result: list[DuplicateGroup] = []
    for h, members in groups.items():
        if len(members) > 1:
            # Canonical = the parser-preferred (lowest index) entry.
            # ``parse_typed_memory`` assigns smaller indexes to
            # higher-priority rows (episodic first, then semantic by
            # confidence desc). The canonical is placed first in
            # ``members`` so downstream consumers see a consistent
            # ordering.
            canonical = min(members, key=lambda x: x.index)
            ordered = sorted(members, key=lambda x: x.index)
            result.append(
                DuplicateGroup(canonical=canonical, members=ordered, reason="exact")
            )
    return result


def find_substring_dupes(
    entries: list[MemoryEntry],
    min_overlap_chars: int = 80,
) -> list[DuplicateGroup]:
    """
    Group entries where one row's text is fully contained in another's.

    Catches "short version" / "long version" pairs. Skips very small
    overlap (under ``min_overlap_chars``) to avoid flagging two
    unrelated rows that share a common phrase.
    """
    result: list[DuplicateGroup] = []
    consumed: set[int] = set()

    # Iterate by text length (longest first) for substring containment,
    # but pick the canonical by parser-preferred index (lowest).
    sorted_entries = sorted(entries, key=lambda e: -len(e.text))

    for i, outer in enumerate(sorted_entries):
        if id(outer) in consumed:
            continue
        inners: list[MemoryEntry] = []
        for inner in sorted_entries[i + 1:]:
            if id(inner) in consumed:
                continue
            if (
                inner.text.strip() in outer.text
                and len(inner.text.strip()) >= min_overlap_chars
            ):
                inners.append(inner)
        if inners:
            for inner in inners:
                consumed.add(id(inner))
            consumed.add(id(outer))
            # Canonical = parser-preferred (lowest index). Recency
            # tiebreak uses confidence desc; if equal, lower index wins.
            group_members = [outer] + inners
            canonical = min(
                group_members,
                key=lambda e: (e.index, -float(e.confidence or 0.0)),
            )
            ordered = sorted(
                group_members,
                key=lambda e: (e.index, -float(e.confidence or 0.0)),
            )
            result.append(
                DuplicateGroup(
                    canonical=canonical,
                    members=ordered,
                    reason="substring",
                )
            )
    return result


def find_common_prefix_dupes(
    entries: list[MemoryEntry],
    min_prefix_chars: int = 60,
) -> list[DuplicateGroup]:
    """
    Find entries sharing a long common prefix. These are often the
    same fact edited over time ("...uses Postgres" → "...uses Postgres
    + pgvector"). Not duplicates per se — flag them for the LLM pass
    to consider merging.
    """
    result: list[DuplicateGroup] = []
    consumed: set[int] = set()
    sorted_entries = sorted(entries, key=lambda e: -len(e.text))

    for i, a in enumerate(sorted_entries):
        if id(a) in consumed:
            continue
        cluster = [a]
        for b in sorted_entries[i + 1:]:
            if id(b) in consumed:
                continue
            prefix_len = 0
            for x, y in zip(a.text, b.text):
                if x.lower() != y.lower():
                    break
                prefix_len += 1
            if prefix_len >= min_prefix_chars:
                cluster.append(b)
                consumed.add(id(b))
        if len(cluster) > 1:
            consumed.add(id(a))
            # Canonical = parser-preferred (lowest index); recency
            # tiebreak uses confidence desc.
            canonical = min(
                cluster,
                key=lambda e: (e.index, -float(e.confidence or 0.0)),
            )
            ordered = sorted(
                cluster,
                key=lambda e: (e.index, -float(e.confidence or 0.0)),
            )
            result.append(
                DuplicateGroup(
                    canonical=canonical,
                    members=ordered,
                    reason="prefix",
                )
            )
    return result


def find_semantic_dupes(
    store: ParsedStore,
    *,
    threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    candidates: int = DEFAULT_VECTOR_CANDIDATES,
    database_url: str | None = None,
) -> list[DuplicateGroup]:
    """
    Find typed_memory rows whose pgvector cosine similarity is at or
    above ``threshold`` AND whose memory_type + category match. Pairs
    are returned as 2-member groups; the canonical is the
    parser-preferred entry (lowest index) with confidence descending
    as a tiebreaker. The canonical is placed first in ``members`` so
    downstream consumers see a consistent ordering.

    Permission scope is taken from ``store.user_id``: only rows
    owned by the same actor are compared. This is a safety property
    — the dream never proposes a merge that crosses an actor
    boundary.
    """
    if not store.entries:
        return []

    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    pairs_seen: set[tuple[str, str]] = set()
    groups: list[DuplicateGroup] = []

    # Build the (memory_type, category) → [entry] index.
    by_bucket: dict[tuple[str, str], list[MemoryEntry]] = {}
    for e in store.entries:
        by_bucket.setdefault((e.memory_type, e.category), []).append(e)

    with psycopg.connect(url, row_factory=dict_row) as conn:
        for entry in store.entries:
            if entry.superseded_by:
                continue
            # Fetch the entry's own embedding once, then bind it as a
            # parameter so the cosine query only needs one round-trip
            # and one embedding lookup per entry (was 3x).
            emb_row = conn.execute(
                "select embedding from memory.typed_memory where id = %s",
                (entry.row_id,),
            ).fetchone()
            if not emb_row or emb_row["embedding"] is None:
                continue
            emb = emb_row["embedding"]
            rows = conn.execute(
                """
                select
                    id,
                    1 - (embedding <=> %s) as similarity
                from memory.typed_memory
                where user_id = %s
                  and memory_type = %s
                  and category = %s
                  and id != %s
                  and superseded_by is null
                  and embedding is not null
                order by embedding <=> %s
                limit %s
                """,
                (
                    emb,
                    entry.user_id,
                    entry.memory_type,
                    entry.category,
                    entry.row_id,
                    emb,
                    candidates,
                ),
            ).fetchall()

            for row in rows:
                sim = float(row["similarity"])
                if sim < threshold:
                    break  # rows are sorted by similarity desc
                other_id = str(row["id"])
                # Sorted pair (canonical, other) — sorted so a→b and b→a collide.
                a_id, b_id = sorted((entry.row_id, other_id))
                key = (a_id, b_id)  # explicit tuple[str, str] for type checker
                if key in pairs_seen:
                    continue
                pairs_seen.add(key)
                # Find the matching MemoryEntry for other_id.
                other = next(
                    (e for e in by_bucket[(entry.memory_type, entry.category)]
                     if e.row_id == other_id),
                    None,
                )
                if other is None:
                    continue
                # Canonical = parser-preferred (lowest index);
                # tiebreak by confidence desc.
                canonical = min(
                    [entry, other],
                    key=lambda e: (e.index, -float(e.confidence or 0.0)),
                )
                members = sorted(
                    [entry, other],
                    key=lambda e: (e.index, -float(e.confidence or 0.0)),
                )
                groups.append(
                    DuplicateGroup(
                        canonical=canonical,
                        members=members,
                        reason="semantic",
                        similarity=sim,
                    )
                )

    return groups


def find_all_dupes(
    store: ParsedStore,
    *,
    include_semantic: bool = True,
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    database_url: str | None = None,
) -> list[DuplicateGroup]:
    """
    Run all dedup passes in order: exact → substring → prefix → semantic.
    Returns non-overlapping groups (entries that are already in a
    group are skipped by later passes).
    """
    groups: list[DuplicateGroup] = []
    consumed_ids: set[str] = set()

    def _consume(g: DuplicateGroup) -> bool:
        if any(m.row_id in consumed_ids for m in g.members):
            return False
        for m in g.members:
            consumed_ids.add(m.row_id)
        return True

    for g in find_exact_dupes(store.entries):
        if _consume(g):
            groups.append(g)
    for g in find_substring_dupes(store.entries):
        if _consume(g):
            groups.append(g)
    for g in find_common_prefix_dupes(store.entries):
        if _consume(g):
            groups.append(g)
    if include_semantic:
        for g in find_semantic_dupes(
            store, threshold=semantic_threshold, database_url=database_url
        ):
            if _consume(g):
                groups.append(g)

    return groups


if __name__ == "__main__":
    import sys

    from parser import parse_typed_memory

    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ACTOR_ID", "u_owner")
    store = parse_typed_memory(user_id=target)
    print(f"=== Dream dedup for user_id={target} ({store.entry_count} entries) ===")
    groups = find_all_dupes(store, include_semantic=False)
    print(f"Found {len(groups)} lexical duplicate groups (semantic pass skipped)")
    for g in groups:
        print(f"\n  {g.reason} (canonical row_id={g.canonical.row_id[:8]}…):")
        for m in g.members:
            print(f"    [{m.memory_type}/{m.category} c={m.confidence:.2f}] {m.text[:60]}…")
