"""Phase 6: Lightweight graph memory in Postgres.

Extracts entities from memory text, links them via edges, and expands
retrieval with 1-2 hop neighbor traversal.

Usage:
    from src.graph_memory import GraphMemory
    gm = GraphMemory()
    gm.extract_and_link(memory_id, "Mo Memory uses Postgres and pgvector.")
    neighbors = gm.expand_graph([memory_id], depth=1, user_id="u_123", org_id="org_123")
"""

from __future__ import annotations

import os
import re
from time import perf_counter
from typing import Any

import psycopg
from psycopg.rows import dict_row

DEFAULT_DATABASE_URL = "postgresql:///agent_memory"

# Simple entity patterns for MVP (no external NLP library)
ENTITY_PATTERNS = [
    # Capitalized phrases (proper nouns, product names)
    re.compile(r"\b[A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)+\b"),
    # Tech stack keywords
    re.compile(r"\b(Postgres|pgvector|LangGraph|LangChain|Python|TypeScript|React|Docker|Kubernetes|Redis|Kafka|WhatsApp|Telegram|OpenAI|Claude|GPT|LLM|API|SQL|NoSQL)\b", re.IGNORECASE),
    # Domain patterns: emails, URLs, phone numbers
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    re.compile(r"\bhttps?://[^\s]+"),
    re.compile(r"\+?\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,4}[-.\s]?\d{1,9}"),
]

# Relationship heuristics: proximity-based + verb-based
# NOTE: edge_type must be one of the schema-valid values:
#   related_to, contradicts, supersedes, supports, derived_from, part_of, references
RELATIONSHIP_MAP = {
    "uses": "references",
    "works_on": "part_of",
    "prefers": "related_to",
    "owns": "derived_from",
    "contains": "part_of",
    "requires": "supports",
    "related_to": "related_to",
}

RELATIONSHIP_VERBS = {
    "uses": ["uses", "use", "using", "built on", "runs on", "deployed on"],
    "works_on": ["works on", "working on", "project", "building", "developing"],
    "prefers": ["prefers", "prefer", "likes", "favorite", "default"],
    "owns": ["owns", "owner", "created by", "managed by"],
    "contains": ["contains", "has", "includes", "consists of"],
    "requires": ["requires", "needs", "depends on", "requires"],
}


def _extract_entities(text: str) -> set[str]:
    """Extract simple entity strings from text."""
    entities: set[str] = set()
    for pattern in ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            ent = match.group().strip()
            if len(ent) > 2:
                entities.add(ent)
    return entities


def _infer_relationships(text: str, entities: list[str]) -> list[tuple[str, str, str, float]]:
    """Infer (source, target, edge_type, weight) from text containing entities.

    Heuristic: if two entities appear within 10 words of each other, and a
    relationship verb is nearby, create an edge.
    """
    edges: list[tuple[str, str, str, float]] = []
    words = text.split()
    ent_positions: dict[str, list[int]] = {}
    for i, w in enumerate(words):
        for ent in entities:
            if ent.lower() in w.lower():
                ent_positions.setdefault(ent, []).append(i)

    for e1, pos1 in ent_positions.items():
        for e2, pos2 in ent_positions.items():
            if e1 >= e2:
                continue
            # Check proximity
            min_dist = min(abs(a - b) for a in pos1 for b in pos2)
            if min_dist > 15:
                continue

            # Check for relationship verbs in the span between them
            span_start = max(0, min(min(pos1), min(pos2)) - 3)
            span_end = min(len(words), max(max(pos1), max(pos2)) + 3)
            span = " ".join(words[span_start:span_end]).lower()

            best_type = "related_to"
            best_weight = 0.3
            for raw_type, verbs in RELATIONSHIP_VERBS.items():
                for verb in verbs:
                    if verb in span:
                        best_type = RELATIONSHIP_MAP.get(raw_type, "related_to")
                        best_weight = 0.7
                        break
                if best_weight > 0.3:
                    break

            edges.append((e1, e2, best_type, best_weight))

    return edges


class GraphMemory:
    """Postgres-backed lightweight graph memory."""

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

    def _connect(self):
        conn = psycopg.connect(self.database_url, row_factory=dict_row)
        conn.execute("SELECT set_config('app.current_role', 'service', false)")
        return conn

    # ------------------------------------------------------------------
    # Entity extraction + linking
    # ------------------------------------------------------------------

    def extract_and_link(self, memory_id: str, text: str) -> list[str]:
        """Extract entities from *text*, store them, and link to *memory_id*."""
        entities = _extract_entities(text)
        if not entities:
            return []

        edge_records = _infer_relationships(text, sorted(entities))
        created_edge_ids: list[str] = []

        with self._connect() as conn:
            # Insert or ignore edges
            for source, target, edge_type, weight in edge_records:
                # Check if edge already exists (bidirectional)
                existing = conn.execute(
                    """
                    SELECT id FROM memory.memory_edges
                    WHERE (source_id = %s AND target_id = %s AND edge_type = %s)
                         OR (source_id = %s AND target_id = %s AND edge_type = %s)
                    LIMIT 1
                    """,
                    (source, target, edge_type, target, source, edge_type),
                ).fetchone()

                if existing:
                    # Update weight (increase confidence)
                    conn.execute(
                        """
                        UPDATE memory.memory_edges
                        SET weight = LEAST(weight + 0.1, 1.0),
                            metadata = jsonb_set(metadata, '{seen_count}',
                                COALESCE((metadata->>'seen_count')::int, 1)::text::jsonb)
                        WHERE id = %s
                        """,
                        (existing["id"],),
                    )
                    created_edge_ids.append(str(existing["id"]))
                else:
                    row = conn.execute(
                        """
                        INSERT INTO memory.memory_edges
                            (source_id, target_id, edge_type, weight, created_by, metadata)
                        VALUES
                            (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            source,
                            target,
                            edge_type,
                            weight,
                            "system",  # created_by must be in ('system', 'agent_inference', 'user_defined')
                            psycopg.types.json.Jsonb({
                                "extracted_by": "graph_memory.extract_and_link",
                                "memory_id": memory_id,
                                "source_text_preview": text[:200],
                            }),
                        ),
                    ).fetchone()
                    created_edge_ids.append(str(row["id"]))

        return created_edge_ids

    # ------------------------------------------------------------------
    # Graph expansion
    # ------------------------------------------------------------------

    def expand_graph(
        self,
        seed_memory_ids: list[str],
        *,
        depth: int = 1,
        limit: int = 10,
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Expand from seed memory IDs through graph edges.

        Returns memory rows that are linked via graph edges to the seeds.
        """
        if not seed_memory_ids:
            return []
        if not user_id:
            raise ValueError("user_id is required for permission-safe graph expansion")

        with self._connect() as conn:
            # Recursive CTE: traverse edges from seed memory entities
            # Note: in this lightweight model, memory_ids are stored in edge metadata.
            # We look for edges where either source or target matches an entity
            # that appears in the seed memory content.
            #
            # For a more robust graph, we'd have a separate entities table.
            # Here we use the edge source_id/target_id as entity names and
            # check if they appear in seed memory content.
            rows = conn.execute(
                """
                WITH seed_entities AS (
                    -- Extract 'words' from seed memory contents as simple entity proxy
                    SELECT DISTINCT unnest(regexp_split_to_array(
                        lower(COALESCE(content, '')), E'\\s+|[^a-z0-9]+'
                    )) AS ent
                    FROM memory.typed_memory
                    WHERE id = ANY(%s)
                      AND user_id = %s
                      AND (%s::text IS NULL OR org_id = %s::text OR visibility = 'owner_only')
                ),
                related_edges AS (
                    SELECT e.*
                    FROM memory.memory_edges e
                    WHERE lower(e.source_id) IN (SELECT ent FROM seed_entities)
                       OR lower(e.target_id) IN (SELECT ent FROM seed_entities)
                ),
                neighbor_entities AS (
                    SELECT DISTINCT
                        CASE WHEN lower(source_id) IN (SELECT ent FROM seed_entities)
                             THEN target_id ELSE source_id END AS neighbor
                    FROM related_edges
                )
                SELECT m.*
                FROM memory.typed_memory m
                WHERE m.user_id = %s
                  AND (%s::text IS NULL OR m.org_id = %s::text OR m.visibility = 'owner_only')
                  AND EXISTS (
                      SELECT 1
                      FROM neighbor_entities ne
                      WHERE lower(m.content) LIKE '%%' || lower(ne.neighbor) || '%%'
                )
                ORDER BY m.confidence DESC, m.created_at DESC
                LIMIT %s
                """,
                (seed_memory_ids, user_id, org_id, org_id, user_id, org_id, org_id, limit),
            ).fetchall()

            return [dict(r) for r in rows]

    def get_related_memories(
        self,
        memory_id: str,
        *,
        depth: int = 1,
        limit: int = 5,
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get memories related to a specific memory via graph edges."""
        if not user_id:
            raise ValueError("user_id is required for permission-safe graph expansion")
        # Fetch the memory content first
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT content
                FROM memory.typed_memory
                WHERE id = %s
                  AND user_id = %s
                  AND (%s::text IS NULL OR org_id = %s::text OR visibility = 'owner_only')
                """,
                (memory_id, user_id, org_id, org_id),
            ).fetchone()
            if not row:
                return []

            entities = _extract_entities(row["content"])
            if not entities:
                return []

            # Find edges where any entity appears as source or target
            entity_list = list(entities)
            rows = conn.execute(
                """
                SELECT DISTINCT
                    CASE
                        WHEN me.source_id = ANY(%s) THEN me.target_id
                        ELSE me.source_id
                    END AS related_entity,
                    me.edge_type,
                    me.weight
                FROM memory.memory_edges me
                WHERE me.source_id = ANY(%s) OR me.target_id = ANY(%s)
                ORDER BY me.weight DESC
                LIMIT %s
                """,
                (entity_list, entity_list, entity_list, limit * 2),
            ).fetchall()

            if not rows:
                return []

            related_entities = [r["related_entity"] for r in rows]
            # Find memories mentioning these related entities
            # Use ILIKE OR chain
            conditions = " OR ".join(
                "m.content ILIKE %s" for _ in related_entities
            )
            patterns = [f"%{e}%" for e in related_entities]
            mem_rows = conn.execute(
                f"""
                SELECT m.id, m.memory_type, m.category, m.content,
                       m.confidence, m.created_at
                FROM memory.typed_memory m
                WHERE m.id != %s
                  AND m.user_id = %s
                  AND (%s::text IS NULL OR m.org_id = %s::text OR m.visibility = 'owner_only')
                  AND ({conditions})
                ORDER BY m.confidence DESC, m.created_at DESC
                LIMIT %s
                """,
                (memory_id, user_id, org_id, org_id, *patterns, limit),
            ).fetchall()

            return [dict(r) for r in mem_rows]
