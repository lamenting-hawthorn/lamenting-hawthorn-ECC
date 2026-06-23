import json
import os

from examples.export_skillloop_trace import SAMPLE_TRACE_ID, run_sample_runtime_turn
from src.trace_export import build_skillloop_trace, build_skillloop_trace_from_state


def test_skillloop_trace_export_redacts_and_hashes_runtime_turn():
    trace = build_skillloop_trace(
        trace_id="trace-1",
        user_text="use api_key=secret123",
        assistant_response="Done with token abc123",
        actor={"actor_id": "u1", "org_id": "o1", "role": "owner", "token": "secret"},
        session_id="s1",
        retrieved_context="private context",
        events=[
            {
                "id": "event-1",
                "step_name": "postgres_retrieval",
                "status": "ok",
                "source": "hybrid",
                "latency_ms": 12,
                "results_count": 2,
            }
        ],
        created_at="2026-06-22T00:00:00+00:00",
    )

    assert trace["source"] == "agent_architecture"
    assert trace["schema_version"] == "1.1"
    assert trace["adapter"]["name"] == "agent_architecture_trace_export"
    assert trace["metadata"]["actor"]["token"] == "[REDACTED]"
    assert "[REDACTED]" in trace["messages"][0]["content"]
    assert trace["messages"][1]["tool_calls"][0]["status"] == "success"
    assert trace["normalized_trace_sha256"]


def test_skillloop_trace_export_requires_actor_in_state():
    try:
        build_skillloop_trace_from_state({"trace_id": "missing-actor"})
    except ValueError as exc:
        assert "actor" in str(exc)
    else:
        raise AssertionError("missing actor should fail closed")


def test_sample_runtime_turn_exports_ingestable_agent_architecture_trace(tmp_path):
    previous_env = {
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "LLM_API_KEY": os.environ.get("LLM_API_KEY"),
        "LLM_BASE_URL": os.environ.get("LLM_BASE_URL"),
        "LLM_MODEL": os.environ.get("LLM_MODEL"),
    }
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/should_not_be_used"
    os.environ["LLM_API_KEY"] = "should_not_be_used"
    os.environ["LLM_BASE_URL"] = "https://example.invalid"
    os.environ["LLM_MODEL"] = "remote-model"

    try:
        output = run_sample_runtime_turn(tmp_path / "sample_runtime_turn_trace.jsonl")
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    records = [
        json.loads(line)
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(records) == 1
    trace = records[0]
    assert trace["id"] == SAMPLE_TRACE_ID
    assert trace["source"] == "agent_architecture"
    assert trace["adapter"]["name"] == "agent_architecture_trace_export"
    assert trace["metadata"]["actor"]["org_id"] == "sample_org"
    assert trace["messages"][0]["role"] == "user"
    assert trace["messages"][1]["role"] == "assistant"

    tool_call_names = {
        call["name"]
        for call in trace["messages"][1]["tool_calls"]
    }
    tool_calls_by_name = {
        call["name"]: call
        for call in trace["messages"][1]["tool_calls"]
    }
    assert {
        "agent_memory_retrieval",
        "model_answer",
        "salience_gate",
    } <= tool_call_names
    assert tool_calls_by_name["agent_memory_retrieval"]["arguments"]["results_count"] == 0
    assert tool_calls_by_name["model_answer"]["status"] == "fallback"
    assert tool_calls_by_name["model_answer"]["success"] is None
    assert tool_calls_by_name["model_answer"]["arguments"]["source"] == "local_fallback"
