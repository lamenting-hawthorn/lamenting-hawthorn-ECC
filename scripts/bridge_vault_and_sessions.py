#!/usr/bin/env python3
"""Vault-first bridge: imports Obsidian vault + SQLite evidence into Postgres.

Design: .hermes/BRIDGE_DESIGN.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
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
DEFAULT_VAULT_PATH = os.path.expanduser("~/Knowledge base")
DEFAULT_SQLITE_PATH = os.path.expanduser("~/.hermes/state.db")
DEFAULT_DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")

# User-confirmed identity defaults
ACTOR_ID = os.environ.get("ACTOR_ID", "owner:<USER>")
ORG_ID = "personal"
ROLE = "owner"
VISIBILITY = "owner_only"
PER_ANCHOR_CAP = 20

# Routing trigger phrases (lowercase, substring match against message content)
PREFERENCE_TRIGGERS = [
    "i prefer", "i like", "i want", "remember", "note that", "save this",
    "my preference", "i always", "i never", "i usually", "i tend to",
]

FACT_TRIGGERS = [
    "the capital is", "is defined as", "is a", "are a", "consists of",
    "works by", "is called", "is named", "was founded", "uses",
    "is built on", "is powered by", "is implemented in",
]

CORRECTION_TRIGGERS = [
    "no,", "actually,", "that's wrong", "incorrect", "you're wrong",
    "that's not", "you forgot", "you missed", "fix this", "change that",
    "actually it's", "i meant",
]

ACTION_TRIGGERS = [
    "todo:", "to-do:", "follow up", "need to", "let's", "i should",
    "we should", "remember to", "don't forget", "make sure",
    "let's do", "we need to",
]


@dataclass
class BridgeConfig:
    vault_path: Path
    sqlite_path: Path
    database_url: str
    mode: str
    dry_run: bool
    verbose: bool
    vault_file_filter: Optional[str] = None

@dataclass
class VaultAnchor:
    path: Path
    rel_path: str
    frontmatter: Dict[str, Any]
    raw_content: str
    content_hash: str
    anchor_strength: float
    suggested_memory_type: str
    suggested_category: str
    keyword_set: List[str]
    date_center: Optional[datetime]
    date_window_days: int

@dataclass
class MatchedChunk:
    anchor: VaultAnchor
    message: Dict[str, Any]
    match_score: float
    session_meta: Dict[str, Any]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()

def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str, str]:
    raw = text
    if not text.startswith("---"):
        return {}, text, raw
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text, raw
    fm_text = parts[1].strip()
    body = parts[2].strip()
    fm: Dict[str, Any] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            fm[key.strip()] = val.strip()
    return fm, body, raw

def _is_excluded_anchor(rel_path: str, frontmatter: Dict[str, Any], raw_content: str) -> bool:
    """Exclude MOCs, indexes, and empty files. READMEs are kept when they
    live under a memory/ subtree — the bridge design treats
    ``memory/procedural/README.md`` and similar as valid anchors."""
    name = Path(rel_path).name.lower()
    rel_lower = rel_path.lower()
    # Exclude by name patterns
    if "moc" in name or "moc" in frontmatter.get("type", "").lower():
        return True
    if name == "index.md":
        return True
    # Only exclude top-level / non-memory README files. A README inside a
    # memory/ subtree is a legitimate anchor.
    if name == "readme.md" and "memory/" not in rel_lower:
        return True
    # Exclude empty or near-empty files
    body = raw_content
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
    if len(body) < 200:
        return True
    # Exclude files with empty frontmatter and no substantive content
    fm_type = frontmatter.get("type", "").strip()
    if not fm_type and len(body) < 400:
        return True
    return False

# Frontmatter / filename date keys the bridge design (§5.5) treats as
# authoritative for the SQLite search window. We try them in priority order
# before falling back to the filesystem mtime.
_FRONTMATTER_DATE_KEYS = ("lastUpdated", "last_updated", "updated", "created", "date")
_DATE_FILENAME_RE = re.compile(r"(20\d{2})[-_](\d{2})[-_](\d{2})")


def _vault_date_center(path: Path, frontmatter: Dict[str, Any], rel_path: str) -> Optional[datetime]:
    """Best-effort ``date_center`` for a vault anchor.

    Resolution order (per bridge design §5.5):

    1. ``lastUpdated`` / ``updated`` / ``created`` in frontmatter.
    2. ISO date embedded in the filename (``2026-05-23.md`` or
       ``2026_05_23.md``) — the format used by daily journals.
    3. ``None`` — caller is expected to fall back to ``st_mtime``.
    """
    # 1. frontmatter
    for key in _FRONTMATTER_DATE_KEYS:
        raw = frontmatter.get(key)
        if not raw:
            continue
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if isinstance(raw, str):
            text = raw.strip().strip("'\"")
            # Try ISO 8601 with optional trailing Z
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                continue
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    # 2. filename (daily journal)
    match = _DATE_FILENAME_RE.search(path.name)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _extract_keywords(anchor: VaultAnchor) -> List[str]:
    keywords: set[str] = set()
    stem = anchor.path.stem.lower().replace("-", " ").replace("_", " ")
    keywords.update(stem.split())
    title = anchor.frontmatter.get("title", "")
    if title:
        keywords.update(title.lower().split())
    # first H1
    for line in anchor.raw_content.splitlines():
        if line.startswith("# "):
            keywords.update(line[2:].lower().split())
            break
    # tags from frontmatter
    tags = anchor.frontmatter.get("tags", "")
    if tags:
        keywords.update(t.strip().lower() for t in tags.strip("[]").split(",") if t.strip())
    # bolded terms
    import re
    for m in re.finditer(r"\*\*(.+?)\*\*", anchor.raw_content):
        keywords.update(m.group(1).lower().split())
    STOPWORDS = {"the", "and", "for", "with", "from", "this", "that", "readme", "moc", "index", "draft"}
    filtered = [k for k in keywords if len(k) > 2 and k not in STOPWORDS]
    # Deduplicate preserving order
    seen: set[str] = set()
    result: List[str] = []
    for k in filtered:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result[:12]

def _classify_vault(rel_path: str, frontmatter: Dict[str, Any]) -> Tuple[str, str, float]:
    fm_type = frontmatter.get("type", "").lower()
    if fm_type == "procedure":
        return "procedural", "procedure", 0.95
    if fm_type == "interaction":
        return "episodic", "interaction", 0.95
    if fm_type == "preference":
        return "semantic", "preference", 0.95
    if fm_type in ("core", "fact", "factual-memory", "entity"):
        return "semantic", "fact", 0.95

    rp = rel_path.lower()
    if "memory/core.md" in rp:
        return "semantic", "fact", 1.0
    if "memory/factual/" in rp:
        return "semantic", "fact", 0.9
    if "memory/procedural/" in rp:
        return "procedural", "procedure", 0.9
    if "memory/episodic/" in rp:
        return "episodic", "interaction", 0.9
    if "wiki/personal/" in rp:
        return "semantic", "preference", 0.85
    if "wiki/projects/" in rp:
        return "semantic", "fact", 0.8
    if "wiki/entities/" in rp:
        return "semantic", "fact", 0.75
    if "wiki/learning/" in rp:
        return "semantic", "fact", 0.6
    if "wiki/concepts/" in rp:
        return "semantic", "fact", 0.5
    if "wiki/company/" in rp:
        return "semantic", "fact", 0.6
    if rp.startswith("2026-") or rp.startswith("2025-"):
        return "episodic", "interaction", 0.7
    if "sources/" in rp or "registry/" in rp or "briefs/" in rp or "dist/" in rp:
        return "semantic", "fact", 0.3
    if "inbox/" in rp:
        return "semantic", "fact", 0.2
    if "_archive/" in rp:
        return "semantic", "fact", 0.0
    return "semantic", "fact", 0.6

def _classify_message(role: str, content: str) -> Tuple[str, str, float]:
    text = content.lower()
    if role == "session_meta":
        return "", "", 0.0
    if role == "assistant":
        trivial = {"ok", "sure", "done", "got it", "yes", "no", "thanks", "alright", "okay"}
        stripped = text.strip().rstrip(".").lower()
        if stripped in trivial or len(text.strip()) < 20:
            return "", "", 0.0
        return "semantic", "fact", 0.5
    if role == "tool":
        # Drop large tool outputs without salient facts
        if len(content) > 10000:
            return "", "", 0.0
        return "semantic", "fact", 0.6
    # user
    for trig in PREFERENCE_TRIGGERS:
        if trig in text:
            return "semantic", "preference", 0.7
    for trig in FACT_TRIGGERS:
        if trig in text:
            return "semantic", "fact", 0.7
    for trig in CORRECTION_TRIGGERS:
        if trig in text:
            return "semantic", "correction", 0.7
    for trig in ACTION_TRIGGERS:
        if trig in text:
            return "episodic", "action_item", 0.7
    return "episodic", "interaction", 0.7

def _make_vault_idempotency_key(anchor: VaultAnchor) -> str:
    return _sha256(f"{anchor.rel_path}|{anchor.content_hash}")

def _make_sqlite_idempotency_key(msg: Dict[str, Any]) -> str:
    ts = msg.get("timestamp", "")
    role = msg.get("role", "")
    content = msg.get("content", "") or ""
    return _sha256(f"<HOME>/.hermes/state.db|{role}|{content}|{ts}")

def _summarize_vault_content(raw: str) -> str:
    """Extract first ~2000 chars of body (after frontmatter) as content."""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            raw = parts[2].strip()
    lines = raw.splitlines()
    # Skip empty lines at start
    while lines and not lines[0].strip():
        lines.pop(0)
    body = "\n".join(lines)
    if len(body) > 2000:
        body = body[:2000] + "\n..."
    return body

# ---------------------------------------------------------------------------
# VaultScanner
# ---------------------------------------------------------------------------
class VaultScanner:
    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path

    def iter_anchors(self) -> Iterator[VaultAnchor]:
        for path in sorted(self.vault_path.rglob("*.md")):
            rel = str(path.relative_to(self.vault_path))
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(json.dumps({"event": "vault_read_error", "path": rel, "error": str(e)}))
                continue
            try:
                fm, _body, raw_full = _parse_frontmatter(raw)
            except Exception:
                fm, _body, raw_full = {}, raw, raw
            # Exclude MOCs, indexes, READMEs, empty files
            if _is_excluded_anchor(rel, fm, raw_full):
                continue
            content_hash = _sha256(raw_full)
            mtype, cat, strength = _classify_vault(rel, fm)
            anchor = VaultAnchor(
                path=path,
                rel_path=rel,
                frontmatter=fm,
                raw_content=raw_full,
                content_hash=content_hash,
                anchor_strength=strength,
                suggested_memory_type=mtype,
                suggested_category=cat,
                keyword_set=[],
                date_center=None,
                date_window_days=7,
            )
            anchor.keyword_set = _extract_keywords(anchor)
            # Date window — prefer the frontmatter/filename date, fall back to
            # file mtime only when nothing else is available. The mtime is the
            # least reliable signal (it shifts for synced or backfilled files)
            # and the bridge design §5.5 explicitly recommends the other
            # sources in priority order.
            date_center = _vault_date_center(path, anchor.frontmatter, rel)
            if date_center is None:
                date_center = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            anchor.date_center = date_center
            if "memory/core.md" in rel.lower():
                anchor.date_window_days = 14
            elif "memory/" in rel.lower():
                anchor.date_window_days = 7
            elif rel.lower().startswith("2026-") or rel.lower().startswith("2025-"):
                anchor.date_window_days = 1
            else:
                anchor.date_window_days = 7
            yield anchor

# ---------------------------------------------------------------------------
# SqliteScanner
# ---------------------------------------------------------------------------
class SqliteScanner:
    def __init__(self, sqlite_path: Path) -> None:
        self.sqlite_path = sqlite_path
        self.conn = sqlite3.connect(str(sqlite_path))
        self.conn.row_factory = sqlite3.Row

    def find_matches(self, anchor: VaultAnchor, incremental_last_id: int = 0) -> List[MatchedChunk]:
        if anchor.anchor_strength < 0.5:
            return []
        # Use top keywords by length (most specific first). An anchor with no
        # usable keywords (e.g. a daily journal with a date in its filename
        # and an empty body) is still valid — fall back to a date-window scan
        # over the SQLite messages so the journal can still pull in evidence
        # from the surrounding day(s).
        keywords: List[str] = []
        if anchor.keyword_set:
            keywords = sorted([k for k in anchor.keyword_set if len(k) > 2], key=len, reverse=True)[:8]

        if not keywords and not anchor.date_center:
            return []

        # Try FTS5 on messages_fts (content column only) when we have keywords.
        rowids: set[int] = set()
        if keywords:
            for kw in keywords[:3]:
                try:
                    cursor = self.conn.execute(
                        "SELECT rowid FROM messages_fts WHERE content MATCH ? LIMIT 200",
                        (kw,),
                    )
                    for r in cursor.fetchall():
                        rowids.add(r[0])
                except Exception:
                    pass

        if not rowids:
            # Fallback LIKE (only useful when we have keywords).
            if keywords:
                likes = [f"%{kw}%" for kw in keywords]
                clauses = " OR ".join(["content LIKE ?"] * len(likes))
                cursor = self.conn.execute(
                    f"SELECT id FROM messages WHERE ({clauses}) AND id > ? AND role != 'session_meta' LIMIT 200",
                    likes + [incremental_last_id],
                )
                for r in cursor.fetchall():
                    rowids.add(r[0])

        # Date-window fallback: if keyword search produced nothing (or we
        # never had keywords to begin with), scan messages by timestamp using
        # the anchor's date_center and date_window_days.
        if not rowids and anchor.date_center is not None:
            center = anchor.date_center
            half_window = anchor.date_window_days * 24 * 3600
            lo = int(center.timestamp()) - half_window
            hi = int(center.timestamp()) + half_window
            try:
                cursor = self.conn.execute(
                    "SELECT id FROM messages WHERE timestamp BETWEEN ? AND ? AND id > ? AND role != 'session_meta' LIMIT 200",
                    (lo, hi, incremental_last_id),
                )
                for r in cursor.fetchall():
                    rowids.add(r[0])
            except Exception:
                pass

        if not rowids:
            return []

        placeholders = ",".join(["?"] * len(rowids))
        # Apply ``incremental_last_id`` to the final lookup too. The FTS path
        # populates ``rowids`` from ``messages_fts.rowid`` regardless of the
        # checkpoint, so without this filter scheduled incremental runs would
        # rescan historical evidence and rely on dedup to recover.
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE id IN ({placeholders}) AND id > ? AND role != 'session_meta'",
            list(rowids) + [incremental_last_id],
        ).fetchall()

        center = anchor.date_center
        date_window = anchor.date_window_days
        chunks: List[MatchedChunk] = []
        for row in rows:
            msg = dict(row)
            if not msg.get("content"):
                continue
            ts = msg.get("timestamp", 0)
            msg_dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            if center and msg_dt:
                delta = abs((msg_dt - center).days)
                if delta > date_window * 2:
                    continue
            score = self._score_match(msg, anchor)
            session_meta = self._get_session_meta(msg.get("session_id", ""))
            chunks.append(MatchedChunk(anchor=anchor, message=msg, match_score=score, session_meta=session_meta))

        # Deduplicate
        seen: set[tuple[str, int]] = set()
        deduped: List[MatchedChunk] = []
        for c in chunks:
            key = (c.message["session_id"], c.message["id"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)

        deduped.sort(key=lambda x: (x.match_score, x.message.get("id", 0)), reverse=True)
        return deduped[:PER_ANCHOR_CAP]

    def _score_match(self, message: Dict[str, Any], anchor: VaultAnchor) -> float:
        score = 1.0
        content = (message.get("content") or "").lower()
        for kw in anchor.keyword_set:
            if kw.lower() in content:
                score += 1.0
        if message.get("role") == "user":
            score += 0.5
        ts = message.get("timestamp", 0)
        if anchor.date_center and ts:
            msg_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            delta_days = abs((msg_dt - anchor.date_center).days)
            if delta_days <= 7:
                score += 1.0
            elif delta_days <= 14:
                score += 0.5
        return score

    def _get_session_meta(self, session_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else {}

    def get_max_message_id(self) -> int:
        row = self.conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0] or 0

    def close(self) -> None:
        self.conn.close()

# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------
class Redactor:
    def redact(self, content: str) -> Tuple[str, Dict[str, str]]:
        result = pseudonymize_payload(content)
        return result.payload, result.reverse_mapping

# ---------------------------------------------------------------------------
# Postgres Writer
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

    def ensure_checkpoint_table(self) -> None:
        if self.dry_run:
            return
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory.bridge_ingest_state (
                    source TEXT PRIMARY KEY,
                    last_session_id TEXT,
                    last_message_id INTEGER,
                    last_run_at TIMESTAMPTZ
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_typed_memory_sqlite_idempotency
                ON memory.typed_memory ((metadata->>'sqlite_idempotency_key'))
                WHERE source IN ('user_utterance', 'agent_inference', 'tool_result')
                """
            )
            # Vault facts get their own partial unique index so two concurrent
            # bridge runs cannot both insert the same anchor between the
            # existence check and the INSERT. ``vault_idempotency_key`` is a
            # stable hash of (rel_path, content_hash) — duplicates must be
            # impossible at the DB layer, not just guarded in Python.
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_typed_memory_vault_idempotency
                ON memory.typed_memory ((metadata->>'vault_idempotency_key'))
                WHERE source IN ('hermes_import', 'vault_bridge')
                  AND metadata ? 'vault_idempotency_key'
                """
            )
            conn.commit()

    def get_checkpoint(self, source: str) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM memory.bridge_ingest_state WHERE source = %s", (source,)
                ).fetchone()
                return dict(row) if row else None
        except psycopg.errors.UndefinedTable:
            return None

    def save_checkpoint(self, source: str, last_message_id: int, stats: Dict[str, Any]) -> None:
        if self.dry_run:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory.bridge_ingest_state (source, last_message_id, last_run_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (source) DO UPDATE SET
                    last_message_id = EXCLUDED.last_message_id,
                    last_run_at = EXCLUDED.last_run_at
                """,
                (source, last_message_id),
            )

    def write_vault_fact(self, anchor: VaultAnchor, redacted: str, reverse_map: Dict[str, str]) -> Optional[str]:
        idempotency_key = _make_vault_idempotency_key(anchor)
        metadata = {"vault_idempotency_key": idempotency_key}
        if reverse_map:
            metadata["redacted"] = True
        if self.dry_run:
            print(json.dumps({
                "event": "dry_run_vault",
                "rel_path": anchor.rel_path,
                "memory_type": anchor.suggested_memory_type,
                "category": anchor.suggested_category,
                "content_preview": redacted[:200],
            }))
            return "dry-run-id"

        with self._connect() as conn:
            try:
                row = conn.execute(
                    """
                    INSERT INTO memory.typed_memory
                        (memory_type, category, content, summary, user_id, session_id,
                         org_id, role, visibility, confidence, source, metadata, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT ((metadata->>'vault_idempotency_key'))
                    WHERE source IN ('hermes_import', 'vault_bridge')
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        anchor.suggested_memory_type,
                        anchor.suggested_category,
                        redacted,
                        None,
                        ACTOR_ID,
                        anchor.rel_path,
                        ORG_ID,
                        ROLE,
                        VISIBILITY,
                        anchor.anchor_strength,
                        "hermes_import",
                        psycopg.types.json.Jsonb(metadata),
                    ),
                ).fetchone()
            except Exception as e:
                conn.rollback()
                self.errored += 1
                print(json.dumps({
                    "event": "vault_error",
                    "rel_path": anchor.rel_path,
                    "error": str(e),
                }))
                return None

            if row is None:
                # Either the ON CONFLICT path or the original pre-check
                # path landed on an existing row. Both should count as
                # a skip, never a duplicate insert.
                self.skipped += 1
                return None

            tm_id = str(row["id"])
            self.inserted += 1

            if reverse_map:
                try:
                    conn.execute(
                        """
                        INSERT INTO memory.audit_log (event_type, user_id, session_id, target_id, details)
                        VALUES ('memory_written', %s, %s, %s, %s)
                        """,
                        (
                            ACTOR_ID,
                            anchor.rel_path,
                            tm_id,
                            psycopg.types.json.Jsonb({
                                "reverse_mapping": reverse_map,
                                "source": "vault",
                                "redacted_by": "bridge_vault_and_sessions.py",
                            }),
                        ),
                    )
                    conn.commit()
                except Exception as e:
                    # Roll the typed_memory insert back too — without the
                    # audit log the row would be orphaned.
                    conn.rollback()
                    self.errored += 1
                    print(json.dumps({
                        "event": "vault_audit_error",
                        "rel_path": anchor.rel_path,
                        "memory_id": tm_id,
                        "error": str(e),
                    }))
                    return None
            return tm_id

    def write_sqlite_evidence(self, chunks: List[MatchedChunk], redacted: str, reverse_map: Dict[str, str], synthetic_content: Optional[str] = None) -> Optional[str]:
        if not chunks:
            return None
        msg = chunks[0].message
        # When the caller passes ``synthetic_content`` (a session-level
        # summary string), classify from *that* text rather than from the
        # arbitrary first chunk. Otherwise the row's memory_type / category /
        # confidence would be driven by a single message, not the summary
        # actually being persisted as ``content``.
        classify_text = synthetic_content if synthetic_content else msg.get("content", "")
        mtype, cat, conf = _classify_message(msg.get("role", ""), classify_text)
        if not mtype:
            return None
        idempotency_key = _make_sqlite_idempotency_key(msg)
        if synthetic_content:
            # Distinguish aggregated-session rows from single-message rows
            # so re-runs of the bridge can detect and dedupe them.
            idempotency_key = f"agg:{idempotency_key}"
        metadata = {"sqlite_idempotency_key": idempotency_key}
        session_id = msg.get("session_id", "")
        role = msg.get("role", "")
        if role == "user":
            source_label = "user_utterance"
        elif role == "tool":
            source_label = "tool_result"
        else:
            source_label = "agent_inference"
        if self.dry_run:
            print(json.dumps({
                "event": "dry_run_sqlite",
                "session_id": session_id,
                "memory_type": mtype,
                "category": cat,
                "content_preview": redacted[:200],
            }))
            return "dry-run-id"

        with self._connect() as conn:
            try:
                row = conn.execute(
                    """
                    INSERT INTO memory.typed_memory
                        (memory_type, category, content, summary, user_id, session_id,
                         org_id, role, visibility, confidence, source, metadata, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT ((metadata->>'sqlite_idempotency_key'))
                    WHERE source IN ('user_utterance', 'agent_inference', 'tool_result')
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        mtype,
                        cat,
                        redacted,
                        None,
                        ACTOR_ID,
                        session_id,
                        ORG_ID,
                        ROLE,
                        VISIBILITY,
                        conf,
                        source_label,
                        psycopg.types.json.Jsonb(metadata),
                    ),
                ).fetchone()
            except psycopg.errors.UniqueViolation:
                row = None

            if row is None:
                self.skipped += 1
                # Try to get existing id for return value
                existing = conn.execute(
                    "SELECT id FROM memory.typed_memory WHERE metadata->>'sqlite_idempotency_key' = %s",
                    (idempotency_key,),
                ).fetchone()
                return str(existing["id"]) if existing else None

            tm_id = str(row["id"])
            self.inserted += 1

            if reverse_map:
                conn.execute(
                    """
                    INSERT INTO memory.audit_log (event_type, user_id, session_id, target_id, details)
                    VALUES ('memory_written', %s, %s, %s, %s)
                    """,
                    (
                        ACTOR_ID,
                        session_id,
                        tm_id,
                        psycopg.types.json.Jsonb({
                            "reverse_mapping": reverse_map,
                            "source": "sqlite",
                            "redacted_by": "bridge_vault_and_sessions.py",
                        }),
                    ),
                )
            return tm_id

    def get_counts(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM memory.typed_memory").fetchone()["c"]
            by_source = conn.execute(
                "SELECT source, COUNT(*) AS c FROM memory.typed_memory GROUP BY source"
            ).fetchall()
            by_type = conn.execute(
                "SELECT memory_type, COUNT(*) AS c FROM memory.typed_memory GROUP BY memory_type"
            ).fetchall()
            return {
                "total": total,
                "by_source": {r["source"]: r["c"] for r in by_source},
                "by_memory_type": {r["memory_type"]: r["c"] for r in by_type},
            }

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

# ---------------------------------------------------------------------------
# BridgeRunner
# ---------------------------------------------------------------------------
class BridgeRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.redactor = Redactor()
        self.writer = PostgresWriter(config.database_url, dry_run=config.dry_run)
        self.stats = {
            "vault_files_scanned": 0,
            "vault_anchors": 0,
            "sqlite_matches": 0,
            "rows_inserted": 0,
            "rows_skipped": 0,
            "rows_errored": 0,
            "runtime_seconds": 0.0,
        }

    def run(self) -> int:
        start = time.time()
        self.writer.ensure_checkpoint_table()

        vault_scanner = VaultScanner(self.config.vault_path)
        sqlite_scanner = SqliteScanner(self.config.sqlite_path)

        checkpoint = self.writer.get_checkpoint("vault")
        incremental_mtime = None
        if self.config.mode == "incremental" and checkpoint:
            last_run = checkpoint.get("last_run_at")
            if last_run:
                incremental_mtime = last_run

        anchors: List[VaultAnchor] = []
        for anchor in vault_scanner.iter_anchors():
            if self.config.vault_file_filter:
                if anchor.rel_path != self.config.vault_file_filter:
                    continue
            # Skip _archive and very low strength by default
            if anchor.anchor_strength >= 0.5:
                self.stats["vault_anchors"] += 1
            if anchor.anchor_strength <= 0.0:
                continue
            if incremental_mtime:
                mtime = datetime.fromtimestamp(anchor.path.stat().st_mtime, tz=timezone.utc)
                if mtime <= incremental_mtime:
                    continue
            anchors.append(anchor)

        self.stats["vault_files_scanned"] = len(anchors)

        incremental_last_id = 0
        if self.config.mode == "incremental":
            cp_sqlite = self.writer.get_checkpoint("sqlite")
            if cp_sqlite:
                incremental_last_id = cp_sqlite.get("last_message_id") or 0

        max_msg_id = sqlite_scanner.get_max_message_id()

        for anchor in anchors:
            if self.config.verbose:
                print(json.dumps({"event": "processing_anchor", "rel_path": anchor.rel_path, "keywords": anchor.keyword_set[:6]}))

            # 1. Write vault fact
            try:
                summary = _summarize_vault_content(anchor.raw_content)
                if not summary or not summary.strip():
                    self.stats["rows_errored"] += 1
                    continue
                redacted, rev_map = self.redactor.redact(summary)
                self.writer.write_vault_fact(anchor, redacted, rev_map)
            except Exception as e:
                self.stats["rows_errored"] += 1
                print(json.dumps({"event": "vault_write_error", "rel_path": anchor.rel_path, "error": str(e)}))
                continue

            # 2. Find SQLite matches (only for strong anchors)
            chunks: List[MatchedChunk] = []
            if anchor.anchor_strength >= 0.5:
                try:
                    chunks = sqlite_scanner.find_matches(anchor, incremental_last_id)
                except Exception as e:
                    print(json.dumps({"event": "sqlite_match_error", "rel_path": anchor.rel_path, "error": str(e)}))
                    chunks = []

            self.stats["sqlite_matches"] += len(chunks)

            # 3. Aggregate per-session and write evidence
            by_session: Dict[str, List[MatchedChunk]] = {}
            for c in chunks:
                sid = c.message.get("session_id", "")
                by_session.setdefault(sid, []).append(c)

            for sid, session_chunks in by_session.items():
                if len(session_chunks) > 5:
                    topics = set()
                    for c in session_chunks:
                        topics.update(c.anchor.keyword_set)
                    summary = (
                        f"Session '{sid}' ({len(session_chunks)} messages). "
                        f"Topics: {', '.join(list(topics)[:5])}."
                    )
                    try:
                        redacted, rev_map = self.redactor.redact(summary)
                        synthetic = MatchedChunk(
                            anchor=anchor,
                            message=session_chunks[0].message,
                            match_score=session_chunks[0].match_score,
                            session_meta=session_chunks[0].session_meta,
                        )
                        # Pass the actual summary text through so the row's
                        # memory_type / category / confidence reflect the
                        # content being persisted, not the first message.
                        self.writer.write_sqlite_evidence([synthetic], redacted, rev_map, synthetic_content=summary)
                    except Exception as e:
                        self.stats["rows_errored"] += 1
                        print(json.dumps({"event": "sqlite_write_error", "session_id": sid, "error": str(e)}))
                else:
                    for c in session_chunks:
                        content = c.message.get("content", "")
                        if not content:
                            continue
                        try:
                            redacted, rev_map = self.redactor.redact(content)
                            self.writer.write_sqlite_evidence([c], redacted, rev_map)
                        except Exception as e:
                            self.stats["rows_errored"] += 1
                            print(json.dumps({"event": "sqlite_write_error", "message_id": c.message.get("id"), "error": str(e)}))

        sqlite_scanner.close()

        # Merge writer tallies into the runner's accumulated counts *before*
        # deciding whether to advance checkpoints. The runner's own
        # ``rows_errored`` includes redaction failures and any per-anchor
        # errors that never made it to the writer.
        self.stats["rows_inserted"] = self.stats.get("rows_inserted", 0) + self.writer.inserted
        self.stats["rows_skipped"] = self.stats.get("rows_skipped", 0) + self.writer.skipped
        self.stats["rows_errored"] = self.stats.get("rows_errored", 0) + self.writer.errored

        # Only advance the checkpoint when the run finished cleanly. A
        # partial-failure run that touched ``max_msg_id`` would otherwise
        # permanently skip every vault file or SQLite message that errored
        # on this run, because the next incremental scan starts past it.
        if not self.config.dry_run and self.stats["rows_errored"] == 0:
            self.writer.save_checkpoint("vault", max_msg_id, self.stats)
            self.writer.save_checkpoint("sqlite", max_msg_id, self.stats)

        self.stats["runtime_seconds"] = round(time.time() - start, 2)

        self._report()
        self.writer.close()

        if self.stats["rows_errored"] > 0 and self.stats["rows_inserted"] == 0:
            return 1
        if self.stats["rows_errored"] > 0:
            return 2
        return 0

    def _report(self) -> None:
        print(json.dumps({"event": "bridge_complete", "stats": self.stats}))

# ---------------------------------------------------------------------------
# Verify mode
# ---------------------------------------------------------------------------
def run_verify(config: BridgeConfig) -> int:
    writer = PostgresWriter(config.database_url, dry_run=False)
    counts1 = writer.get_counts()
    print(json.dumps({"event": "verify_pre", "counts": counts1}))

    full_config = BridgeConfig(
        vault_path=config.vault_path,
        sqlite_path=config.sqlite_path,
        database_url=config.database_url,
        mode="full",
        dry_run=False,
        verbose=config.verbose,
    )
    runner = BridgeRunner(full_config)
    runner.run()
    counts2 = writer.get_counts()
    print(json.dumps({"event": "verify_post_first", "counts": counts2}))

    runner2 = BridgeRunner(full_config)
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
            SELECT metadata->>'vault_idempotency_key' AS k, COUNT(*) AS c
            FROM memory.typed_memory
            WHERE source = 'hermes_import'
            GROUP BY metadata->>'vault_idempotency_key'
            HAVING COUNT(*) > 1
            """
        ).fetchall()
    if dups:
        print(json.dumps({"event": "verify_fail", "reason": "duplicate vault idempotency keys", "duplicates": len(dups)}))
        ok = False
    else:
        print(json.dumps({"event": "verify_ok", "message": "No duplicate vault idempotency keys"}))

    writer.close()
    return 0 if ok else 1

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Vault-first bridge")
    parser.add_argument("--mode", choices=["full", "incremental", "verify"], default="full")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--vault-path", default=DEFAULT_VAULT_PATH)
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE_PATH)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--vault-file-filter", default=None)
    args = parser.parse_args()

    config = BridgeConfig(
        vault_path=Path(args.vault_path),
        sqlite_path=Path(args.sqlite_path),
        database_url=args.database_url,
        mode=args.mode,
        dry_run=args.dry_run,
        verbose=args.verbose,
        vault_file_filter=args.vault_file_filter,
    )

    if args.mode == "verify":
        return run_verify(config)

    runner = BridgeRunner(config)
    return runner.run()

if __name__ == "__main__":
    sys.exit(main())
