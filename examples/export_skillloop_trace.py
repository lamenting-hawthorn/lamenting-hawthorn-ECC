#!/usr/bin/env python3
"""Run a local runtime turn and export it as a SkillLoop-compatible trace."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.graph import build_graph
from src.trace_export import build_skillloop_trace_from_state, write_trace_jsonl


DEFAULT_OUTPUT = ROOT / "examples" / "out" / "sample_runtime_turn_trace.jsonl"
SAMPLE_TRACE_ID = "sample-runtime-turn-001"
SAMPLE_SESSION_ID = "sample-skillloop-session"
SAMPLE_ACTOR = {
    "actor_id": "sample_user",
    "org_id": "sample_org",
    "role": "owner",
}
SAMPLE_USER_TEXT = (
    "Remember this: Agent Architecture should prefer local embeddings before "
    "API embeddings."
)


def _with_default_env() -> dict[str, str | None]:
    previous = {
        "MEMORY_BACKEND": os.environ.get("MEMORY_BACKEND"),
        "CHECKPOINTER": os.environ.get("CHECKPOINTER"),
        "AGENT_ORG_ID": os.environ.get("AGENT_ORG_ID"),
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "TRACE_STORE_DISABLED": os.environ.get("TRACE_STORE_DISABLED"),
        "LLM_API_KEY": os.environ.get("LLM_API_KEY"),
        "LLM_BASE_URL": os.environ.get("LLM_BASE_URL"),
        "LLM_MODEL": os.environ.get("LLM_MODEL"),
    }
    os.environ["MEMORY_BACKEND"] = "fake"
    os.environ["CHECKPOINTER"] = "memory"
    os.environ["AGENT_ORG_ID"] = SAMPLE_ACTOR["org_id"]
    os.environ["TRACE_STORE_DISABLED"] = "1"
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("LLM_API_KEY", None)
    os.environ.pop("LLM_BASE_URL", None)
    os.environ["LLM_MODEL"] = "local_fallback"
    return previous


def _restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _sample_events(result: dict[str, Any]) -> list[dict[str, Any]]:
    retrieved_context = result.get("retrieved_context")
    fake_no_results = retrieved_context in {"", None, "No durable memory retrieved yet."}
    return [
        {
            "id": "sample-event-retrieve-memory",
            "step_name": "agent_memory_retrieval",
            "status": "ok",
            "source": "fake_memory",
            "latency_ms": 0,
            "results_count": 0 if fake_no_results else None,
        },
        {
            "id": "sample-event-model-answer",
            "step_name": "model_answer",
            "status": "fallback",
            "source": os.environ.get("LLM_MODEL", "local_fallback"),
            "latency_ms": 0,
            "results_count": None,
            "error_message": "LLM_API_KEY was suppressed for the offline sample; used fallback response.",
        },
        {
            "id": "sample-event-memory-write",
            "step_name": "salience_gate",
            "status": "ok",
            "source": "runtime_memory_policy",
            "latency_ms": 0,
            "results_count": len(result.get("memory_writes", [])),
        },
    ]


def run_sample_runtime_turn(output: Path = DEFAULT_OUTPUT) -> Path:
    """Run one local graph turn and write the SkillLoop JSONL artifact."""
    previous_env = _with_default_env()
    try:
        graph = build_graph()
        result = graph.invoke(
            {
                "trace_id": SAMPLE_TRACE_ID,
                "user_text": SAMPLE_USER_TEXT,
                "actor": SAMPLE_ACTOR,
                "session_id": SAMPLE_SESSION_ID,
                "messages": [],
                "memory_writes": [],
                "written_memory_ids": [],
            },
            config={"configurable": {"thread_id": SAMPLE_SESSION_ID}},
        )
        trace = build_skillloop_trace_from_state(result, events=_sample_events(result))
        return write_trace_jsonl(trace, output)
    finally:
        _restore_env(previous_env)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a local Agent Architecture turn and export a SkillLoop trace."
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT),
        help="Output JSONL path for the SkillLoop trace export.",
    )
    args = parser.parse_args()

    output = run_sample_runtime_turn(Path(args.out))
    print(f"Wrote SkillLoop trace export: {output}")
    print()
    print("Ingest from a SkillLoop checkout with:")
    print(f"  skillloop --path . ingest agent-architecture {output}")
    print()
    print("Expected output:")
    print(f"  Ingested agent_architecture trace {SAMPLE_TRACE_ID} (2 messages)")


if __name__ == "__main__":
    main()
