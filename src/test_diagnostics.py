#!/usr/bin/env python3
"""Verify trace events and approval-gated diagnostics."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tracing import TraceEventInput, TraceStore, build_diagnostic_report, new_trace_id


def main() -> None:
    store = TraceStore()
    marker = uuid4().hex[:8]
    trace_id = new_trace_id(f"diagnostic-{marker}")
    user_id = f"diag_user_{marker}"
    org_id = f"diag_org_{marker}"
    session_id = f"diag_session_{marker}"

    store.log_event(
        TraceEventInput(
            trace_id=trace_id,
            step_name="agent_memory_retrieval",
            status="ok",
            source="hermes_internal_memory",
            user_id=user_id,
            org_id=org_id,
            session_id=session_id,
            latency_ms=2,
            results_count=0,
        )
    )
    store.log_event(
        TraceEventInput(
            trace_id=trace_id,
            step_name="postgres_retrieval",
            status="ok",
            source="hybrid_retrieval",
            user_id=user_id,
            org_id=org_id,
            session_id=session_id,
            latency_ms=31,
            results_count=0,
        )
    )
    store.log_event(
        TraceEventInput(
            trace_id=trace_id,
            step_name="wiki_retrieval",
            status="error",
            source="llmwiki_mcp",
            user_id=user_id,
            org_id=org_id,
            session_id=session_id,
            latency_ms=400,
            error_message="tool_choice unsupported by selected model",
        )
    )

    events = store.list_events(trace_id)
    report = build_diagnostic_report(events)
    report_id = store.create_diagnostic_report(
        trace_id=trace_id,
        user_id=user_id,
        org_id=org_id,
        session_id=session_id,
        report=report,
    )

    assert len(events) == 3
    assert report_id
    assert "wiki_retrieval failed" in report["issue_summary"]
    assert report["next_steps"][0]["status"] == "pending_human_approval"
    assert len(report["independent_reviews"]) == 2
    assert {r["reviewer"] for r in report["independent_reviews"]} == {
        "trace_analyzer_subagent",
        "fix_planner_subagent",
    }

    print(f"trace_id: {trace_id}")
    print(f"diagnostic_report_id: {report_id}")
    print(f"issue: {report['issue_summary']}")
    print(f"fix: {report['proposed_fix']}")
    print("diagnostics test passed")


if __name__ == "__main__":
    os.environ.setdefault("DATABASE_URL", "postgresql:///agent_memory")
    main()
