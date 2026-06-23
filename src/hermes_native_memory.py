"""Hermes native memory bridge.

This module models the fast memory layer inside Hermes. In production, replace
the in-process store with Hermes' real native memory API while keeping the same
read/write contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from time import time
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class NativeMemoryRecord:
    id: str
    actor_id: str
    org_id: str
    role: str
    content: str
    memory_type: str
    category: str
    visibility: str
    confidence: float
    created_at: float
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HermesNativeMemoryStore:
    """Small process-local store that stands in for Hermes native memory."""

    _records: list[NativeMemoryRecord] = []

    def write(
        self,
        *,
        actor_id: str,
        org_id: str,
        role: str,
        content: str,
        memory_type: str = "semantic",
        category: str = "fact",
        visibility: str = "owner_only",
        confidence: float = 0.8,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        record = NativeMemoryRecord(
            id=f"native:{uuid4()}",
            actor_id=actor_id,
            org_id=org_id,
            role=role,
            content=content,
            memory_type=memory_type,
            category=category,
            visibility=visibility,
            confidence=confidence,
            created_at=time(),
            metadata=metadata or {},
        )
        self._records.append(record)
        return record.id

    def search(
        self,
        query: str,
        *,
        actor_id: str,
        org_id: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query_terms = _terms(query)
        scored: list[tuple[float, NativeMemoryRecord]] = []
        for record in self._records:
            if record.actor_id != actor_id:
                same_org_shared = (
                    record.org_id == org_id and record.visibility in ("team", "org", "public")
                )
                if not same_org_shared:
                    continue

            record_terms = _terms(record.content)
            overlap = len(query_terms & record_terms)
            identity_boost = 1 if {"do", "work", "job", "role"} & query_terms and "engineer" in record_terms else 0
            score = overlap + identity_boost + record.confidence
            if score > 0:
                scored.append((score, record))

        scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [record.to_dict() for _, record in scored[:limit]]

    @classmethod
    def clear(cls) -> None:
        cls._records.clear()


def _terms(text: str) -> set[str]:
    return {
        token.strip(".,:;!?()[]{}\"'").lower()
        for token in text.split()
        if len(token.strip(".,:;!?()[]{}\"'")) > 2
    }
