"""Local embedding generation for Mo Memory.

Uses sentence-transformers (all-MiniLM-L6-v2) for fast local inference.
No API key needed. Embeddings are padded to 1536 dimensions to match
the Postgres schema (original design target was OpenAI text-embedding-3-small).

Install:
    pip install sentence-transformers

Usage:
    from src.local_embeddings import LocalEmbedder
    embedder = LocalEmbedder()
    vec = embedder.encode("Hello world")  # 1536-dim float list
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np

# Pad local 384-dim embeddings to 1536 to match schema without migration
TARGET_DIM = 1536


class LocalEmbedder:
    """Lazy-loaded local sentence-transformers embedder."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or os.environ.get(
            "LOCAL_EMBEDDING_MODEL", "all-MiniLM-L6-v2"
        )
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers not installed. "
                    "Run: pip install sentence-transformers"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode(self, text: str) -> list[float]:
        model = self._load()
        vec = model.encode(text, normalize_embeddings=True)
        # Pad to TARGET_DIM with zeros so cosine similarity is unchanged
        if len(vec) < TARGET_DIM:
            padded = np.zeros(TARGET_DIM, dtype=np.float32)
            padded[: len(vec)] = vec
            return padded.tolist()
        return vec.tolist()[:TARGET_DIM]

    @property
    def dimensions(self) -> int:
        return TARGET_DIM


# Global singleton (lazy)
_embedder: LocalEmbedder | None = None


def get_embedder() -> LocalEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = LocalEmbedder()
    return _embedder


def encode_text(text: str) -> list[float]:
    return get_embedder().encode(text)
