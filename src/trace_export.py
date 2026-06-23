"""SkillLoop-compatible trace export helpers.

The runtime remains the source of execution truth. This module exposes a small
read-only export boundary so governance tools can evaluate completed traces
without writing back into runtime memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4


TRACE_SCHEMA_VERSION = "1.1"
ADAPTER_NAME = "agent_architecture_trace_export"
ADAPTER_VERSION = "1.0"
RUNTIME_NAME = "agent_architecture"

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]+=*"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_text(value: Any) -> str:
    text = "" if value is None else str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_data(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)(api[_-]?key|token|secret|password)", key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact_data(item)
        return redacted
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


@dataclass(frozen=True)
class ExportedToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    success: bool | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    status: str = "unknown"
    error_type: str | None = None
    artifact_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        status = self.status
        if status == "unknown" and self.success is True:
            status = "success"
        elif status == "unknown" and self.success is False:
            status = "error"

        return {
            "id": self.id,
            "name": redact_text(self.name),
            "arguments": redact_data(self.arguments),
            "result": redact_text(self.result) if self.result is not None else None,
            "success": self.success,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "status": status,
            "error_type": redact_text(self.error_type) if self.error_type else None,
            "artifact_refs": [redact_text(ref) for ref in self.artifact_refs],
        }


def tool_call_from_trace_event(event: dict[str, Any]) -> ExportedToolCall:
    status = str(event.get("status") or "unknown")
    success = True if status == "ok" else False if status == "error" else None
    exported_status = (
        "success"
        if status == "ok"
        else "error"
        if status == "error"
        else status
    )
    return ExportedToolCall(
        id=str(event.get("id") or uuid4().hex),
        name=str(event.get("step_name") or "runtime_step"),
        arguments={
            "source": event.get("source"),
            "results_count": event.get("results_count"),
        },
        result=event.get("error_message"),
        success=success,
        duration_ms=event.get("latency_ms"),
        status=exported_status,
        error_type="runtime_error" if status == "error" else None,
    )


def _trace_hash_payload(trace: dict[str, Any]) -> dict[str, Any]:
    payload = dict(trace)
    payload.pop("raw_trace_sha256", None)
    payload.pop("normalized_trace_sha256", None)
    return payload


def build_skillloop_trace(
    *,
    trace_id: str,
    user_text: str,
    assistant_response: str,
    actor: dict[str, Any],
    session_id: str,
    retrieved_context: str = "",
    events: list[dict[str, Any]] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a SkillLoop `AgentTrace`-compatible dict from a completed turn."""
    safe_actor = redact_data(actor)
    safe_events = [redact_data(event) for event in events or []]
    tool_calls = [tool_call_from_trace_event(event).to_dict() for event in safe_events]

    trace = {
        "id": str(trace_id),
        "schema_version": TRACE_SCHEMA_VERSION,
        "source": "agent_architecture",
        "created_at": created_at or now_iso(),
        "runtime": {"name": RUNTIME_NAME},
        "adapter": {"name": ADAPTER_NAME, "version": ADAPTER_VERSION},
        "metadata": {
            "session_id": str(session_id),
            "actor": safe_actor,
            "retrieval": {
                "context_present": bool(retrieved_context),
                "context_sha256": sha256_text(redact_text(retrieved_context)) if retrieved_context else None,
            },
        },
        "raw_artifact_ref": None,
        "messages": [
            {
                "role": "user",
                "content": redact_text(user_text),
                "tool_calls": [],
                "metadata": {"session_id": str(session_id)},
            },
            {
                "role": "assistant",
                "content": redact_text(assistant_response),
                "tool_calls": tool_calls,
                "metadata": {
                    "retrieved_context": redact_text(retrieved_context),
                    "trace_event_count": len(safe_events),
                },
            },
        ],
    }
    trace["normalized_trace_sha256"] = sha256_text(stable_json_dumps(_trace_hash_payload(trace)))
    trace["raw_trace_sha256"] = trace["normalized_trace_sha256"]
    return trace


def build_skillloop_trace_from_state(
    state: dict[str, Any],
    *,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    actor = state.get("actor")
    if not isinstance(actor, dict):
        raise ValueError("state must include actor before exporting a trace")
    return build_skillloop_trace(
        trace_id=str(state.get("trace_id") or uuid4().hex),
        user_text=str(state.get("user_text") or ""),
        assistant_response=str(state.get("assistant_response") or ""),
        actor=actor,
        session_id=str(state.get("session_id") or "unknown-session"),
        retrieved_context=str(state.get("retrieved_context") or ""),
        events=events,
    )


def write_trace_jsonl(trace: dict[str, Any], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(trace, ensure_ascii=False) + "\n", encoding="utf-8")
    return output
