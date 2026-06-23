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
