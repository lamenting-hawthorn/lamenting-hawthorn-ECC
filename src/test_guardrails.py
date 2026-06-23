#!/usr/bin/env python3
"""Verify loop stop and context compaction guardrails."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.guardrails import (
    ContextCompactionPolicy,
    ToolCallBudget,
    ToolFailureBudgetExceeded,
)


def test_tool_failure_budget() -> None:
    budget = ToolCallBudget(max_failures=5)
    for attempt in range(1, 5):
        count = budget.record_failure("llmwiki.query", RuntimeError("temporary failure"))
        assert count == attempt

    try:
        budget.record_failure("llmwiki.query", RuntimeError("still failing"))
    except ToolFailureBudgetExceeded as exc:
        assert "failed 5 consecutive times" in str(exc)
    else:
        raise AssertionError("Expected ToolFailureBudgetExceeded on fifth failure")

    budget.record_success("llmwiki.query")
    assert "llmwiki.query" not in budget.failures_by_tool
    print("OK: tool failure budget stops after five failures")


def test_context_compaction_policy() -> None:
    policy = ContextCompactionPolicy(
        max_context_tokens=1_000,
        threshold=0.80,
        recent_messages_to_keep=3,
    )
    messages = [
        {"role": "system", "content": "SYSTEM PROMPT MUST STAY"},
        {"role": "user", "content": "old user " + ("x" * 800)},
        {"role": "assistant", "content": "old assistant " + ("y" * 800)},
        {"role": "user", "content": "recent one " + ("a" * 800)},
        {"role": "assistant", "content": "recent two " + ("b" * 800)},
        {"role": "user", "content": "recent three " + ("c" * 800)},
    ]

    result = policy.compact(
        messages,
        current_project_info="agent architecture public repo",
    )

    assert result.compacted
    assert result.estimated_tokens_before >= 800
    assert result.estimated_tokens_after < result.estimated_tokens_before
    assert result.messages[0]["content"] == "SYSTEM PROMPT MUST STAY"
    assert "Current project: agent architecture public repo" in result.messages[1]["content"]
    assert [m["role"] for m in result.messages[-3:]] == ["user", "assistant", "user"]
    print("OK: context compaction preserves system prompt, project info, and recent turns")


def main() -> None:
    test_tool_failure_budget()
    test_context_compaction_policy()
    print("guardrails test passed")


if __name__ == "__main__":
    main()
