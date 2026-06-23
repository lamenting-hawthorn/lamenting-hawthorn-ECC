"""Trace capture and diagnostic approval scaffolding.

This module is intentionally deterministic. It records what happened, builds a
short diagnostic report from trace facts, and leaves all corrective actions in a
pending-human-approval state.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from time import perf_counter
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row


DEFAULT_DATABASE_URL = "postgresql:///agent_memory"


def new_trace_id(prefix: str = "trace") -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


@dataclass(frozen=True)
class TraceEventInput:
    trace_id: str
    step_name: str
    status: str
    user_id: str
    session_id: str
    org_id: str | None = None
    source: str = "system"
    latency_ms: int | None = None
    results_count: int | None = None
    error_message: str | None = None
    details: dict[str, Any] | None = None


class TraceStore:
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

    def _connect(self):
        conn = psycopg.connect(self.database_url, row_factory=dict_row)
        conn.execute("SELECT set_config('app.current_role', 'service', false)")
        return conn

    def log_event(self, event: TraceEventInput) -> str | None:
        """Best-effort trace logging.

        Tracing must never break the user path. If the database is not migrated
        yet, callers continue normally and diagnostics can be enabled later.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO memory.trace_events
                        (trace_id, step_name, status, source, user_id, org_id,
                         session_id, latency_ms, results_count, error_message,
                         details)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        event.trace_id,
                        event.step_name,
                        event.status,
                        event.source,
                        event.user_id,
                        event.org_id,
                        event.session_id,
                        event.latency_ms,
                        event.results_count,
                        event.error_message,
                        psycopg.types.json.Jsonb(event.details or {}),
                    ),
                ).fetchone()
                return str(row["id"])
        except Exception:
            return None

    def list_events(self, trace_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM memory.trace_events
                WHERE trace_id = %s
                ORDER BY created_at, id
                """,
                (trace_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def create_diagnostic_report(
        self,
        *,
        trace_id: str,
        user_id: str,
        session_id: str,
        org_id: str | None = None,
        report: dict[str, Any],
    ) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO memory.diagnostic_reports
                    (trace_id, user_id, org_id, session_id, issue_summary,
                     trace_summary, proposed_fix, independent_reviews,
                     next_steps, approval_status)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_human_approval')
                RETURNING id
                """,
                (
                    trace_id,
                    user_id,
                    org_id,
                    session_id,
                    report["issue_summary"],
                    report["trace_summary"],
                    report["proposed_fix"],
                    psycopg.types.json.Jsonb(report["independent_reviews"]),
                    psycopg.types.json.Jsonb(report["next_steps"]),
                ),
            ).fetchone()
            return str(row["id"])


def timed_event(
    store: TraceStore,
    *,
    trace_id: str,
    step_name: str,
    user_id: str,
    session_id: str,
    org_id: str | None,
    source: str,
    fn,
):
    start = perf_counter()
    try:
        value = fn()
    except Exception as exc:
        store.log_event(
            TraceEventInput(
                trace_id=trace_id,
                step_name=step_name,
                status="error",
                source=source,
                user_id=user_id,
                org_id=org_id,
                session_id=session_id,
                latency_ms=int((perf_counter() - start) * 1000),
                error_message=str(exc)[:500],
            )
        )
        raise

    results_count = len(value) if isinstance(value, list) else None
    store.log_event(
        TraceEventInput(
            trace_id=trace_id,
            step_name=step_name,
            status="ok",
            source=source,
            user_id=user_id,
            org_id=org_id,
            session_id=session_id,
            latency_ms=int((perf_counter() - start) * 1000),
            results_count=results_count,
        )
    )
    return value


def build_diagnostic_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a short, approval-gated diagnostic from trace facts."""
    errors = [event for event in events if event.get("status") == "error"]
    empty_retrievals = [
        event for event in events
        if event.get("step_name", "").endswith("retrieval")
        and int(event.get("results_count") or 0) == 0
    ]
    slow_steps = [
        event for event in events
        if event.get("latency_ms") is not None and int(event["latency_ms"]) > 5000
    ]

    if errors:
        issue = f"{errors[0]['step_name']} failed: {errors[0].get('error_message') or 'unknown error'}"
    elif empty_retrievals:
        issue = "Retrieval returned no stored context before answer generation."
    elif slow_steps:
        issue = f"{slow_steps[0]['step_name']} was slow at {slow_steps[0]['latency_ms']} ms."
    else:
        issue = "No blocking trace issue detected."

    trace_summary = "; ".join(
        f"{event['step_name']}={event['status']}"
        + (
            f"/results:{event['results_count']}"
            if event.get("results_count") is not None
            else ""
        )
        + (
            f"/{event['latency_ms']}ms"
            if event.get("latency_ms") is not None
            else ""
        )
        for event in events
    ) or "No trace events recorded."

    if errors:
        fix = "Replay the failed step with the same trace_id, inspect provider/config errors, then patch only the failing adapter or routing rule."
    elif empty_retrievals:
        fix = "Check agent memory first, then Postgres, then llmwiki; add a routing hint or import missing wiki context only after review."
    elif slow_steps:
        fix = "Profile the slow step and add timeout/caching/provider fallback before changing memory content."
    else:
        fix = "No fix proposed."

    independent_reviews = [
        {
            "reviewer": "trace_analyzer_subagent",
            "role": "find root cause from trace facts only",
            "finding": issue,
        },
        {
            "reviewer": "fix_planner_subagent",
            "role": "propose minimal gated remediation",
            "finding": fix,
        },
    ]

    next_steps = [
        {
            "action": "review_diagnostic_report",
            "requires_human_approval": True,
            "status": "pending_human_approval",
        },
        {
            "action": "apply_minimal_fix_after_approval",
            "requires_human_approval": True,
            "status": "blocked_until_approved",
        },
    ]

    return {
        "issue_summary": issue,
        "trace_summary": trace_summary,
        "proposed_fix": fix,
        "independent_reviews": independent_reviews,
        "next_steps": next_steps,
    }
