#!/usr/bin/env python3
"""SkillLoop-to-Postgres connector: imports approved memory proposals into typed_memory.

Design: .hermes/SKILLOOP_CONNECTOR_DESIGN.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from redaction import pseudonymize_payload

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_PROJECT_ROOT = os.path.expanduser("<HOME>/agent_architecture")
DEFAULT_SKILLLOOP_ROOT = os.path.expanduser("<HOME>/skillloop")
DEFAULT_DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
DEFAULT_MIN_SCORE = 70
DEFAULT_AUTO_APPLY_THRESHOLD = 85

ACTOR_ID = os.environ.get("ACTOR_ID", "owner:<USER>")
ORG_ID = "personal"
ROLE = "owner"
VISIBILITY = "owner_only"

VALID_MEMORY_TYPES = ("episodic", "semantic", "procedural")
VALID_CATEGORIES = (
    "fact", "preference", "interaction", "action_item",
    "correction", "procedure", "knowledge_base", "org_approved",
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ConnectorConfig:
    project_root: Path
    skillloop_root: Path
    database_url: str
    mode: str
    dry_run: bool
    verbose: bool
    min_score: int
    auto_apply_threshold: int

@dataclass
class SkillLoopProposal:
    proposal_id: str
    content: str
    score: int = 0
    trace_id: str = "skillloop_import"
    evaluator: str = "unknown"
    suggested_memory_type: Optional[str] = None
    suggested_category: Optional[str] = None
    source: str = "skillloop_proposal"
    created_at: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_hex_id(stem: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{32}", stem.lower()))


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from markdown text.

    Returns (frontmatter_dict, body). If no frontmatter, returns ({}, text).
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_text = parts[1].strip()
    body = parts[2].strip()
    
    # Simple YAML parser for flat key:value and simple lists
    fm: Dict[str, Any] = {}
    current_key: Optional[str] = None
    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Check if it's a list item under current key
        if stripped.startswith("-") and current_key is not None:
            val = stripped[1:].strip()
            if current_key not in fm:
                fm[current_key] = []
            if isinstance(fm[current_key], list):
                fm[current_key].append(val)
            continue
        # Key: value
        if ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()
            current_key = key
            # Try int
            try:
                fm[key] = int(val)
                continue
            except ValueError:
                pass
            # Try float
            try:
                fm[key] = float(val)
                continue
            except ValueError:
                pass
            # String (strip quotes)
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            fm[key] = val
    return fm, body


def _classify(content: str, tags: List[str], suggested_mtype: Optional[str], suggested_cat: Optional[str]) -> Tuple[str, str]:
    """Layered classification: frontmatter -> tag override -> heuristic -> default."""
    text = content.lower()
    
    # Layer 1: frontmatter suggestions if valid
    mtype = suggested_mtype if suggested_mtype in VALID_MEMORY_TYPES else None
    cat = suggested_cat if suggested_cat in VALID_CATEGORIES else None
    
    # Layer 3: tag overrides
    tag_set = {t.lower() for t in tags}
    if "preference" in tag_set:
        mtype, cat = "semantic", "preference"
    elif "workflow" in tag_set or "procedure" in tag_set:
        mtype, cat = "procedural", "procedure"
    
    if mtype and cat:
        return mtype, cat
    
    # Layer 2: heuristics
    if any(t in text for t in ("i prefer", "i like", "i want", "always", "never")):
        return mtype or "semantic", cat or "preference"
    if any(t in text for t in ("when ", "then ", "first ", "next ", "steps", "workflow")):
        return mtype or "procedural", cat or "procedure"
    if any(t in text for t in ("yesterday", "last week", "session", "interaction", "conversation")):
        return mtype or "episodic", cat or "interaction"
    if any(t in text for t in ("correct", "mistake", "wrong", "should be")):
        return mtype or "semantic", cat or "correction"
    if any(t in text for t in ("todo", "action item", "task", "remind me")):
        return mtype or "semantic", cat or "action_item"
    
    # Default
    return mtype or "semantic", cat or "fact"


def _score_to_confidence(score: int) -> float:
    return min(score / 100.0, 0.95)


# ---------------------------------------------------------------------------
# SkillLoopReader
# ---------------------------------------------------------------------------
class SkillLoopReader:
    def __init__(self, project_root: Path, skillloop_root: Path) -> None:
        self.approved_dir = project_root / ".skillloop" / "approved" / "memory"
        self.db_path = skillloop_root / ".skillloop" / "skillloop.db"
        self._db_cache: Optional[sqlite3.Connection] = None

    def _db(self) -> Optional[sqlite3.Connection]:
        if self._db_cache is None and self.db_path.exists():
            self._db_cache = sqlite3.connect(str(self.db_path))
            self._db_cache.row_factory = sqlite3.Row
        return self._db_cache

    def _lookup_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        db = self._db()
        if db is None:
            return None
        row = db.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _parse_proposal_payload(self, payload: str) -> Dict[str, Any]:
        try:
            return json.loads(payload)
        except Exception:
            return {}

    def iter_proposals(self) -> Iterator[SkillLoopProposal]:
        if not self.approved_dir.exists():
            return
        for path in sorted(self.approved_dir.glob("*.md")):
            stem = path.stem
            if not _is_hex_id(stem):
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(json.dumps({"event": "read_error", "path": str(path), "error": str(e)}))
                continue
            
            fm, body = _parse_frontmatter(raw)
            content = body.strip()
            if not content:
                print(json.dumps({"event": "skipped_empty", "proposal_id": stem}))
                continue
            
            proposal = SkillLoopProposal(
                proposal_id=fm.get("proposal_id") or stem,
                content=content,
                score=fm.get("score") or 0,
                trace_id=fm.get("trace_id") or "skillloop_import",
                evaluator=fm.get("evaluator") or "unknown",
                suggested_memory_type=fm.get("suggested_memory_type"),
                suggested_category=fm.get("suggested_category"),
                source="skillloop_proposal",
                created_at=fm.get("created_at") or datetime.now(timezone.utc).isoformat(),
                tags=fm.get("tags") or [],
                evidence=fm.get("evidence") or [],
            )
            
            # If frontmatter is missing key fields, try SQLite backfill
            if proposal.score == 0 or proposal.trace_id == "skillloop_import":
                db_row = self._lookup_proposal(stem)
                if db_row:
                    _payload = self._parse_proposal_payload(db_row.get("payload", "{}"))
                    if proposal.score == 0:
                        # Try to get score from evaluations table via trace_id
                        db = self._db()
                        if db:
                            ev = db.execute(
                                "SELECT score FROM evaluations WHERE trace_id = ? ORDER BY created_at DESC LIMIT 1",
                                (db_row.get("trace_id", ""),)
                            ).fetchone()
                            if ev:
                                proposal.score = ev["score"]
                    if proposal.trace_id == "skillloop_import":
                        proposal.trace_id = db_row.get("trace_id") or "skillloop_import"
                    if proposal.evaluator == "unknown":
                        proposal.evaluator = "rubric"
            
            yield proposal

    def iter_auto_apply(self, threshold: int) -> Iterator[SkillLoopProposal]:
        """Query skillloop.db for requires_review proposals with score >= threshold.

        Since the current schema doesn't have a 'requires_review' status, we look for
        'pending' proposals with matching evaluations.
        """
        db = self._db()
        if db is None:
            return
        
        rows = db.execute(
            """
            SELECT p.id, p.trace_id, p.kind, p.status, p.payload,
                   e.score, e.payload as eval_payload
            FROM proposals p
            LEFT JOIN evaluations e ON p.trace_id = e.trace_id
            WHERE p.status = 'pending' AND p.kind = 'memory'
            ORDER BY e.created_at DESC
            """
        ).fetchall()
        
        seen: set[str] = set()
        for row in rows:
            proposal_id = row["id"]
            if proposal_id in seen:
                continue
            seen.add(proposal_id)
            score = row["score"] or 0
            if score < threshold:
                continue
            payload = self._parse_proposal_payload(row.get("payload", "{}"))
            content = payload.get("content", "").strip()
            if not content:
                continue
            yield SkillLoopProposal(
                proposal_id=proposal_id,
                content=content,
                score=score,
                trace_id=row.get("trace_id") or "skillloop_import",
                evaluator="rubric",
                suggested_memory_type=None,
                suggested_category=None,
                source="skillloop_proposal",
                created_at=datetime.now(timezone.utc).isoformat(),
            )

    def close(self) -> None:
        if self._db_cache:
            self._db_cache.close()
            self._db_cache = None


# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------
class Redactor:
    def redact(self, content: str) -> Tuple[str, Dict[str, str]]:
        result = pseudonymize_payload(content)
        return result.payload, result.reverse_mapping


# ---------------------------------------------------------------------------
# PostgresWriter
# ---------------------------------------------------------------------------
class PostgresWriter:
    def __init__(self, url: str, dry_run: bool = False) -> None:
        self.url = url
        self.dry_run = dry_run
        self.inserted = 0
        self.skipped = 0
        self.errored = 0
        self._conn: Optional[psycopg.Connection] = None

    def _connect(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.url, row_factory=dict_row)
            self._conn.execute("SELECT set_config('app.current_role', 'service', false)")
        return self._conn

    def ensure_schema(self) -> None:
        if self.dry_run:
            return
        with self._connect() as conn:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_typed_memory_skillloop_idempotency
                ON memory.typed_memory ((metadata->>'skillloop_idempotency_key'))
                WHERE source = 'skillloop_proposal'
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory.skillloop_imports (
                    proposal_id TEXT PRIMARY KEY,
                    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    memory_id UUID REFERENCES memory.typed_memory(id)
                )
                """
            )
            conn.commit()

    def _already_exists(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM memory.typed_memory WHERE metadata->>'skillloop_idempotency_key' = %s AND source = 'skillloop_proposal'",
                (key,),
            ).fetchone()
            return row is not None

    def write_memory(self, proposal: SkillLoopProposal, redacted: str, reverse_map: Dict[str, str]) -> Optional[str]:
        key = proposal.proposal_id
        mtype, cat = _classify(proposal.content, proposal.tags, proposal.suggested_memory_type, proposal.suggested_category)
        confidence = _score_to_confidence(proposal.score)
        metadata = {
            "skillloop_proposal_id": proposal.proposal_id,
            "skillloop_evaluator": proposal.evaluator,
            "skillloop_score": proposal.score,
            "skillloop_evidence": proposal.evidence,
            "skillloop_tags": proposal.tags,
            "skillloop_trace_id": proposal.trace_id,
            "skillloop_idempotency_key": key,
            "redacted": bool(reverse_map),
            "redacted_by": "connect_skillloop.py",
        }
        
        if self.dry_run:
            print(json.dumps({
                "event": "dry_run",
                "proposal_id": proposal.proposal_id,
                "memory_type": mtype,
                "category": cat,
                "content_preview": redacted[:200],
                "confidence": confidence,
            }))
            return "dry-run-id"

        if self._already_exists(key):
            self.skipped += 1
            print(json.dumps({"event": "memory_skipped", "proposal_id": proposal.proposal_id, "reason": "already_exists"}))
            return None

        with self._connect() as conn:
            try:
                row = conn.execute(
                    """
                    INSERT INTO memory.typed_memory
                        (memory_type, category, content, summary, user_id, session_id,
                         org_id, role, visibility, confidence, source, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT ((metadata->>'skillloop_idempotency_key'))
                    WHERE source = 'skillloop_proposal'
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        mtype,
                        cat,
                        redacted,
                        None,
                        ACTOR_ID,
                        proposal.trace_id,
                        ORG_ID,
                        ROLE,
                        VISIBILITY,
                        confidence,
                        "skillloop_proposal",
                        psycopg.types.json.Jsonb(metadata),
                    ),
                ).fetchone()
            except Exception as e:
                self.errored += 1
                print(json.dumps({"event": "memory_error", "proposal_id": proposal.proposal_id, "error": str(e)}))
                return None

            if row is None:
                self.skipped += 1
                print(json.dumps({"event": "memory_skipped", "proposal_id": proposal.proposal_id, "reason": "already_exists"}))
                return None

            tm_id = str(row["id"])
            self.inserted += 1

            # Audit log
            conn.execute(
                """
                INSERT INTO memory.audit_log (event_type, user_id, session_id, target_id, details)
                VALUES ('memory_written', %s, %s, %s, %s)
                """,
                (
                    ACTOR_ID,
                    proposal.trace_id,
                    tm_id,
                    psycopg.types.json.Jsonb({
                        "skillloop_proposal_id": proposal.proposal_id,
                        "source": "skillloop",
                        "memory_type": mtype,
                        "category": cat,
                    }),
                ),
            )
            
            # Track import
            conn.execute(
                """
                INSERT INTO memory.skillloop_imports (proposal_id, memory_id)
                VALUES (%s, %s)
                ON CONFLICT (proposal_id) DO NOTHING
                """,
                (proposal.proposal_id, tm_id),
            )
            
            conn.commit()
            print(json.dumps({"event": "memory_written", "proposal_id": proposal.proposal_id, "memory_id": tm_id, "memory_type": mtype, "category": cat}))
            return tm_id

    def get_counts(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM memory.typed_memory").fetchone()["c"]
            by_source = conn.execute(
                "SELECT source, memory_type, COUNT(*) AS c FROM memory.typed_memory GROUP BY source, memory_type"
            ).fetchall()
            return {
                "total": total,
                "by_source": {f"{r['source']}_{r['memory_type']}": r["c"] for r in by_source},
            }

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()


# ---------------------------------------------------------------------------
# ConnectorRunner
# ---------------------------------------------------------------------------
class ConnectorRunner:
    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config
        self.redactor = Redactor()
        self.reader = SkillLoopReader(config.project_root, config.skillloop_root)
        self.writer = PostgresWriter(config.database_url, dry_run=config.dry_run)
        self.stats = {
            "files_found": 0,
            "rows_inserted": 0,
            "rows_skipped": 0,
            "rows_errored": 0,
            "runtime_seconds": 0.0,
        }

    def run(self) -> int:
        start = time.time()
        self.writer.ensure_schema()

        proposals: List[SkillLoopProposal] = []
        
        # Read approved files
        for proposal in self.reader.iter_proposals():
            self.stats["files_found"] += 1
            if proposal.score < self.config.min_score:
                self.stats["rows_skipped"] += 1
                if self.config.verbose:
                    print(json.dumps({"event": "memory_skipped", "proposal_id": proposal.proposal_id, "reason": "score_below_threshold", "score": proposal.score}))
                continue
            proposals.append(proposal)
        
        # Read auto-apply queue (Path B)
        if self.config.mode == "full":
            for proposal in self.reader.iter_auto_apply(self.config.auto_apply_threshold):
                # Skip if already in approved list
                if any(p.proposal_id == proposal.proposal_id for p in proposals):
                    continue
                if proposal.score < self.config.auto_apply_threshold:
                    continue
                proposals.append(proposal)
                self.stats["files_found"] += 1
        
        # Filter by checkpoint (incremental)
        if self.config.mode == "incremental":
            with self.writer._connect() as conn:
                imported = conn.execute(
                    "SELECT proposal_id FROM memory.skillloop_imports"
                ).fetchall()
                imported_ids = {r["proposal_id"] for r in imported}
            proposals = [p for p in proposals if p.proposal_id not in imported_ids]

        for proposal in proposals:
            if self.config.verbose:
                print(json.dumps({"event": "processing", "proposal_id": proposal.proposal_id, "trace_id": proposal.trace_id, "score": proposal.score}))
            try:
                redacted, rev_map = self.redactor.redact(proposal.content)
            except Exception as e:
                self.stats["rows_errored"] += 1
                print(json.dumps({"event": "redact_error", "proposal_id": proposal.proposal_id, "error": str(e)}))
                continue
            try:
                self.writer.write_memory(proposal, redacted, rev_map)
            except Exception as e:
                self.stats["rows_errored"] += 1
                print(json.dumps({"event": "write_error", "proposal_id": proposal.proposal_id, "error": str(e)}))
                continue

        self.stats["rows_inserted"] = self.writer.inserted
        self.stats["rows_skipped"] = self.writer.skipped
        self.stats["rows_errored"] = self.writer.errored
        self.stats["runtime_seconds"] = round(time.time() - start, 2)

        self._report()
        self.reader.close()
        self.writer.close()

        if self.stats["rows_errored"] > 0 and self.stats["rows_inserted"] == 0:
            return 1
        if self.stats["rows_errored"] > 0:
            return 2
        return 0

    def _report(self) -> None:
        print(json.dumps({"event": "connector_complete", "stats": self.stats}))


# ---------------------------------------------------------------------------
# Verify mode
# ---------------------------------------------------------------------------
def run_verify(config: ConnectorConfig) -> int:
    writer = PostgresWriter(config.database_url, dry_run=False)
    counts1 = writer.get_counts()
    print(json.dumps({"event": "verify_pre", "counts": counts1}))

    full_config = ConnectorConfig(
        project_root=config.project_root,
        skillloop_root=config.skillloop_root,
        database_url=config.database_url,
        mode="full",
        dry_run=False,
        verbose=config.verbose,
        min_score=config.min_score,
        auto_apply_threshold=config.auto_apply_threshold,
    )
    runner = ConnectorRunner(full_config)
    runner.run()
    counts2 = writer.get_counts()
    print(json.dumps({"event": "verify_post_first", "counts": counts2}))

    runner2 = ConnectorRunner(full_config)
    runner2.run()
    counts3 = writer.get_counts()
    print(json.dumps({"event": "verify_post_second", "counts": counts3}))

    ok = True
    new_rows = counts3["total"] - counts2["total"]
    if new_rows != 0:
        print(json.dumps({"event": "verify_fail", "reason": "counts changed on re-run", "before": counts2["total"], "after": counts3["total"], "new_rows": new_rows}))
        ok = False
    else:
        print(json.dumps({"event": "verify_ok", "message": "Idempotent: second run produced 0 new rows"}))

    with writer._connect() as conn:
        dups = conn.execute(
            """
            SELECT metadata->>'skillloop_idempotency_key' AS k, COUNT(*) AS c
            FROM memory.typed_memory
            WHERE source = 'skillloop_proposal'
            GROUP BY metadata->>'skillloop_idempotency_key'
            HAVING COUNT(*) > 1
            """
        ).fetchall()
    if dups:
        print(json.dumps({"event": "verify_fail", "reason": "duplicate skillloop idempotency keys", "duplicates": len(dups)}))
        ok = False
    else:
        print(json.dumps({"event": "verify_ok", "message": "No duplicate skillloop idempotency keys"}))

    writer.close()
    return 0 if ok else 3


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="SkillLoop-to-Postgres connector")
    parser.add_argument("--mode", choices=["full", "incremental", "dry-run", "verify"], default="full")
    parser.add_argument("--project-root", default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--skillloop-root", default=DEFAULT_SKILLLOOP_ROOT)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE)
    parser.add_argument("--auto-apply-threshold", type=int, default=DEFAULT_AUTO_APPLY_THRESHOLD)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    config = ConnectorConfig(
        project_root=Path(args.project_root),
        skillloop_root=Path(args.skillloop_root),
        database_url=args.database_url,
        mode=args.mode if args.mode != "dry-run" else "full",
        dry_run=(args.mode == "dry-run"),
        verbose=args.verbose,
        min_score=args.min_score,
        auto_apply_threshold=args.auto_apply_threshold,
    )

    if args.mode == "verify":
        return run_verify(config)

    runner = ConnectorRunner(config)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
