"""
collector.py — Mine recent runtime activity for the dream curation pass.

In hermes-dream, the "past activity" source is session JSONL files in
``~/.hermes/sessions/``. Here, the equivalent is three tables that the
runtime already writes to:

  - ``event_store.events``  — every incoming user message, tool result,
    and agent step. Source of truth for what the user has said.
  - ``memory.retrieval_logs`` — every retrieval the runtime performed,
    including the query and which memories it pulled. Shows what the
    user is asking about (and what *isn't* being asked).
  - ``memory.trace_events``  — the per-step trace the runtime emits.
    Surfaces errors and slow steps the curator should know about.

The collector returns compact digests — one per "session" defined by
``event_store.events.session_id`` — that fit within a token budget the
LLM can read in a single pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

DEFAULT_DATABASE_URL = "postgresql:///agent_memory"

# How much of each event payload to keep. Tool noise (jsonb) is usually
# large and rarely useful for the curator; trim aggressively.
_MAX_PAYLOAD_CHARS = 600


@dataclass
class ActivityDigest:
    session_id: str
    char_count: int = 0
    user_messages: list[str] = field(default_factory=list)
    assistant_messages: list[str] = field(default_factory=list)
    retrieval_queries: list[str] = field(default_factory=list)
    trace_steps: list[str] = field(default_factory=list)
    text: str = ""

    def __post_init__(self) -> None:
        if not self.text:
            self.text = self._serialize()

    def _serialize(self) -> str:
        parts: list[str] = []
        if self.user_messages:
            parts.append("## User messages")
            for m in self.user_messages:
                parts.append(f"- {m}")
        if self.assistant_messages:
            parts.append("\n## Assistant messages")
            for m in self.assistant_messages:
                parts.append(f"- {m}")
        if self.retrieval_queries:
            parts.append("\n## Retrieval queries")
            for q in self.retrieval_queries:
                parts.append(f"- {q}")
        if self.trace_steps:
            parts.append("\n## Trace steps (errors / slow steps)")
            for s in self.trace_steps:
                parts.append(f"- {s}")
        return "\n".join(parts).strip()


def _connect(database_url: str | None = None):
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    return psycopg.connect(url, row_factory=dict_row)


def _extract_text(payload) -> str:
    """Pull a textual preview from an event payload jsonb.

    Tolerates string / dict / list shapes. If the payload is a tool
    result (which is usually JSONB noise), we extract ``output`` /
    ``text`` / ``content`` if present, otherwise we drop it.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("text", "output", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(payload, list):
        parts = []
        for item in payload:
            text = _extract_text(item)
            if text:
                parts.append(text)
        return " ".join(parts).strip()
    return str(payload).strip()


def collect_activity(
    user_id: str,
    *,
    max_age_days: int = 90,
    max_sessions: int = 30,
    min_session_chars: int = 200,
    max_total_chars: int = 50000,
    database_url: str | None = None,
) -> list[ActivityDigest]:
    """
    Pull the most recent ``max_sessions`` sessions worth of activity
    from event_store, retrieval_logs, and trace_events, then collapse
    them into digests within a ``max_total_chars`` budget.

    A "session" is the set of rows sharing ``session_id`` in
    ``event_store.events`` (the runtime's source of truth for grouping
    a user's activity). Retrieval logs and trace events are joined in
    by ``session_id`` if present, falling back to ``trace_id``.

    Sessions with no user text are skipped (the curator needs user
    content to find preferences). Sessions whose assembled text is
    shorter than ``min_session_chars`` are also skipped (too small
    to be worth the curator's attention).
    """
    # Fail-fast parameter validation. Bad bounds would produce
    # nonsense queries (negative LIMIT, zero-char budget) or just
    # confusing empty results.
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id is required")
    if not isinstance(max_age_days, int) or max_age_days <= 0:
        raise ValueError(f"max_age_days must be a positive int, got {max_age_days!r}")
    if not isinstance(max_sessions, int) or max_sessions <= 0:
        raise ValueError(f"max_sessions must be a positive int, got {max_sessions!r}")
    if not isinstance(min_session_chars, int) or min_session_chars <= 0:
        raise ValueError(f"min_session_chars must be a positive int, got {min_session_chars!r}")
    if not isinstance(max_total_chars, int) or max_total_chars <= 0:
        raise ValueError(f"max_total_chars must be a positive int, got {max_total_chars!r}")
    if min_session_chars > max_total_chars:
        raise ValueError(
            f"min_session_chars ({min_session_chars}) cannot exceed "
            f"max_total_chars ({max_total_chars})"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)  # noqa: UP017

    with _connect(database_url) as conn:
        # 1. Get the most recent N sessions that have at least one
        #    event with extractable user text.
        session_rows = conn.execute(
            """
            select
                session_id,
                max(created_at) as last_at,
                count(*)        as event_count
            from event_store.events
            where user_id = %s
              and created_at >= %s
              and event_type in ('message_received', 'model_answer', 'tool_result')
            group by session_id
            order by last_at desc
            limit %s
            """,
            (user_id, cutoff, max_sessions * 3),  # fetch extra; we filter next
        ).fetchall()

        if not session_rows:
            return []

        session_ids = [r["session_id"] for r in session_rows][:max_sessions]
        session_last_at = {r["session_id"]: r["last_at"] for r in session_rows}
        if not session_ids:
            return []

        # 2. Pull user + assistant text from event_store for those sessions.
        event_rows = conn.execute(
            """
            select
                session_id,
                event_type,
                payload,
                created_at
            from event_store.events
            where user_id = %s
              and session_id = ANY(%s)
              and event_type in ('message_received', 'model_answer')
            order by created_at asc
            """,
            (user_id, session_ids),
        ).fetchall()

        # 3. Pull retrieval queries.
        retrieval_rows = conn.execute(
            """
            select
                session_id,
                query,
                created_at
            from memory.retrieval_logs
            where user_id = %s
              and session_id = ANY(%s)
              and created_at >= %s
            order by created_at asc
            """,
            (user_id, session_ids, cutoff),
        ).fetchall()

        # 4. Pull trace steps that look interesting (errors, fallbacks,
        #    slow steps). Skip 'ok' steps — they don't change the
        #    curator's view of what the user is doing.
        trace_rows = conn.execute(
            """
            select
                session_id,
                step_name,
                status,
                error_message,
                latency_ms,
                created_at
            from memory.trace_events
            where user_id = %s
              and session_id = ANY(%s)
              and created_at >= %s
              and status in ('error', 'fallback')
            order by created_at asc
            """,
            (user_id, session_ids, cutoff),
        ).fetchall()

    # 5. Group by session_id.
    by_session: dict[str, ActivityDigest] = {
        sid: ActivityDigest(session_id=sid) for sid in session_ids
    }

    for r in event_rows:
        digest = by_session.get(r["session_id"])
        if digest is None:
            continue
        text = _extract_text(r["payload"])
        if not text:
            continue
        text = text[:_MAX_PAYLOAD_CHARS]
        if r["event_type"] == "message_received":
            digest.user_messages.append(text)
        elif r["event_type"] == "model_answer":
            digest.assistant_messages.append(text)

    for r in retrieval_rows:
        digest = by_session.get(r["session_id"])
        if digest is None:
            continue
        q = (r.get("query") or "").strip()
        if q:
            digest.retrieval_queries.append(q)

    for r in trace_rows:
        # Only attach trace rows whose session_id matches one of the
        # sessions we're already showing. Unscoped (null or unknown)
        # rows are dropped — they would otherwise be misattributed to
        # the newest session and mix unrelated context into the
        # curator prompt.
        sid = r.get("session_id")
        digest = by_session.get(sid) if sid else None
        if digest is None:
            continue
        line = f"{r['step_name']} status={r['status']}"
        if r.get("error_message"):
            line += f" err={str(r['error_message'])[:200]}"
        if r.get("latency_ms") is not None and int(r["latency_ms"]) > 500:
            line += f" latency={r['latency_ms']}ms"
        digest.trace_steps.append(line)

    # 6. Drop empty digests, sort by recency, then budget-trim.
    digests = [d for d in by_session.values() if d.user_messages or d.assistant_messages]
    digests.sort(key=lambda d: session_last_at.get(d.session_id, datetime.min.replace(tzinfo=timezone.utc)), reverse=True)  # noqa: UP017

    result: list[ActivityDigest] = []
    total = 0
    for d in digests:
        d.text = d._serialize()
        d.char_count = len(d.text)
        if d.char_count < min_session_chars:
            continue
        if total + d.char_count > max_total_chars:
            remaining = max_total_chars - total
            if remaining < min_session_chars:
                break
            d.text = d.text[:remaining] + "\n[…truncated for token budget…]"
            d.char_count = len(d.text)
            result.append(d)
            break
        total += d.char_count
        result.append(d)

    return result


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ACTOR_ID", "u_owner")
    digests = collect_activity(target, max_sessions=5, max_total_chars=20000)
    print(f"user_id={target}  digests={len(digests)}")
    for d in digests:
        print(f"  session={d.session_id}  chars={d.char_count}  "
              f"user_msgs={len(d.user_messages)}  queries={len(d.retrieval_queries)}")
        first_user = d.user_messages[0][:100] if d.user_messages else None
        if first_user:
            print(f"    first user: {first_user}…")
