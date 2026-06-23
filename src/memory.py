"""Phase 1 fake memory layer.

This module intentionally does not connect to Postgres. It proves the graph
shape and the memory write proposal path before durable memory exists.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import re
from typing import Any


FAKE_RETRIEVED_CONTEXT = "No durable memory retrieved yet."


@dataclass(frozen=True)
class ProposedMemoryWrite:
    actor_id: str
    org_id: str
    memory_type: str
    category: str
    text: str
    source: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def retrieve_fake_memory() -> str:
    return FAKE_RETRIEVED_CONTEXT


def should_propose_memory_write(user_text: str) -> bool:
    lowered = user_text.lower()
    triggers = ("remember this", "save this", "note that")
    identity_fact_patterns = (
        r"\bi am an?\s+[\w -]{3,80}",
        r"\bi'm an?\s+[\w -]{3,80}",
        r"\bmy role is\s+[\w -]{3,80}",
        r"\bmy job is\s+[\w -]{3,80}",
        r"\bi work as an?\s+[\w -]{3,80}",
    )
    return (
        lowered.startswith("remember:")
        or any(trigger in lowered for trigger in triggers)
        or any(re.search(pattern, lowered) for pattern in identity_fact_patterns)
    )


def build_memory_write(actor: dict[str, str], user_text: str) -> dict[str, Any]:
    return ProposedMemoryWrite(
        actor_id=actor["actor_id"],
        org_id=actor["org_id"],
        memory_type="semantic",
        category="fact",
        text=user_text.strip(),
        source="phase1_salience_gate",
        confidence=0.8,
    ).to_dict()
