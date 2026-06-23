#!/usr/bin/env python3
"""Create a sample SkillLoop-compatible trace export."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.trace_export import build_skillloop_trace, write_trace_jsonl


def main() -> None:
    trace = build_skillloop_trace(
        trace_id="example-trace-001",
        user_text="Remember that local embeddings are preferred.",
        assistant_response="Recorded. Local embeddings are the default path.",
        actor={"actor_id": "example_user", "org_id": "example_org", "role": "owner"},
        session_id="example-session",
        retrieved_context="No durable memory retrieved yet.",
        events=[
            {
                "id": "example-event-001",
                "step_name": "model_answer",
                "status": "ok",
                "source": "local_fallback",
                "latency_ms": 4,
                "results_count": None,
            }
        ],
        created_at="2026-06-22T00:00:00+00:00",
    )
    output = ROOT / "examples" / "out" / "sample_trace.jsonl"
    write_trace_jsonl(trace, output)
    print(output)


if __name__ == "__main__":
    main()
