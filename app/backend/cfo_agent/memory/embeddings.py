"""Minimal embeddings module needed by seed — full memory package in Phase 2."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


@lru_cache
def _model():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=MODEL_NAME)


def get_embedding_dim() -> int:
    return EMBEDDING_DIM


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = _model()
    vectors = list(model.embed(texts))
    out: list[list[float]] = []
    for v in vectors:
        arr = np.asarray(v, dtype=np.float32)
        assert arr.shape[0] == EMBEDDING_DIM
        out.append(arr.tolist())
    return out


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
