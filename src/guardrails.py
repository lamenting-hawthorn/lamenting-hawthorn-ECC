"""Runtime guardrails for tool loops and context size.

The current graph is mostly linear, but future Hermes/MCP/wiki loops need shared
limits so failures do not spin forever and long conversations preserve the
system/project anchors before compaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Callable


DEFAULT_TOOL_FAILURE_LIMIT = 5
DEFAULT_CONTEXT_COMPACTION_THRESHOLD = 0.80
DEFAULT_MAX_CONTEXT_TOKENS = 128_000


class ToolFailureBudgetExceeded(RuntimeError):
    """Raised when a tool/step exceeds the allowed consecutive failures."""


@dataclass
class ToolCallBudget:
    """Track consecutive failures per tool and fail closed after a limit."""

    max_failures: int = DEFAULT_TOOL_FAILURE_LIMIT
    failures_by_tool: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ToolCallBudget":
        raw = os.environ.get("AGENT_TOOL_FAILURE_LIMIT", "")
        try:
            limit = int(raw) if raw else DEFAULT_TOOL_FAILURE_LIMIT
        except ValueError:
            limit = DEFAULT_TOOL_FAILURE_LIMIT
        return cls(max_failures=max(1, limit))

    def record_success(self, tool_name: str) -> None:
        self.failures_by_tool.pop(tool_name, None)

    def record_failure(self, tool_name: str, error: Exception | str) -> int:
        count = self.failures_by_tool.get(tool_name, 0) + 1
        self.failures_by_tool[tool_name] = count
        if count >= self.max_failures:
            raise ToolFailureBudgetExceeded(
                f"{tool_name} failed {count} consecutive times; stopping loop. "
                f"Last error: {str(error)[:240]}"
            )
        return count

    def run(self, tool_name: str, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
        except Exception as exc:
            self.record_failure(tool_name, exc)
            raise
        self.record_success(tool_name)
        return value


@dataclass(frozen=True)
class ContextCompactionResult:
    messages: list[dict[str, str]]
    compacted: bool
    estimated_tokens_before: int
    estimated_tokens_after: int
    preserved_system_messages: int


@dataclass
class ContextCompactionPolicy:
    """Deterministic 80% context compaction policy.

    This does not summarize with an LLM. It creates a concise handoff marker and
    keeps system/project anchors plus recent turns. A production summarizer can
    replace `build_compaction_note` later, but the preservation rules should stay.
    """

    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    threshold: float = DEFAULT_CONTEXT_COMPACTION_THRESHOLD
    recent_messages_to_keep: int = 12

    @classmethod
    def from_env(cls) -> "ContextCompactionPolicy":
        max_raw = os.environ.get("AGENT_MAX_CONTEXT_TOKENS", "")
        threshold_raw = os.environ.get("AGENT_CONTEXT_COMPACT_AT", "")
        try:
            max_tokens = int(max_raw) if max_raw else DEFAULT_MAX_CONTEXT_TOKENS
        except ValueError:
            max_tokens = DEFAULT_MAX_CONTEXT_TOKENS
        try:
            threshold = float(threshold_raw) if threshold_raw else DEFAULT_CONTEXT_COMPACTION_THRESHOLD
        except ValueError:
            threshold = DEFAULT_CONTEXT_COMPACTION_THRESHOLD
        return cls(
            max_context_tokens=max(1_000, max_tokens),
            threshold=min(max(threshold, 0.1), 0.95),
        )

    def estimate_tokens(self, messages: list[dict[str, str]]) -> int:
        # Conservative enough for routing decisions without binding to one tokenizer.
        chars = sum(len(message.get("content", "")) for message in messages)
        return max(1, chars // 4)

    def should_compact(self, messages: list[dict[str, str]]) -> bool:
        return self.estimate_tokens(messages) >= int(self.max_context_tokens * self.threshold)

    def compact(
        self,
        messages: list[dict[str, str]],
        *,
        current_project_info: str,
    ) -> ContextCompactionResult:
        before = self.estimate_tokens(messages)
        if not self.should_compact(messages):
            return ContextCompactionResult(
                messages=messages,
                compacted=False,
                estimated_tokens_before=before,
                estimated_tokens_after=before,
                preserved_system_messages=sum(1 for m in messages if m.get("role") == "system"),
            )

        system_messages = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        recent = non_system[-self.recent_messages_to_keep:]
        older_count = max(0, len(non_system) - len(recent))

        compacted_messages = [
            *system_messages,
            {
                "role": "system",
                "content": self.build_compaction_note(
                    current_project_info=current_project_info,
                    older_message_count=older_count,
                ),
            },
            *recent,
        ]
        after = self.estimate_tokens(compacted_messages)
        return ContextCompactionResult(
            messages=compacted_messages,
            compacted=True,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            preserved_system_messages=len(system_messages),
        )

    def build_compaction_note(self, *, current_project_info: str, older_message_count: int) -> str:
        return (
            "Context compacted automatically because estimated usage crossed "
            f"{int(self.threshold * 100)}% of the context budget.\n"
            "Preserve all original system/developer instructions above.\n"
            f"Current project: {current_project_info.strip()}\n"
            f"Older non-system messages compacted: {older_message_count}.\n"
            "Use trace_events, diagnostic_reports, architecture docs, and current repo "
            "files to recover exact operational context when needed."
        )
