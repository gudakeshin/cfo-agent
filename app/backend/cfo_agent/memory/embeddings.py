"""Local text embeddings (fastembed, CPU). Text never leaves the deployment for
embedding. The model is read from FASTEMBED_CACHE_PATH (baked into the Docker image)
so no download is needed at runtime."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # vector tables are sized for this; other models must match


@lru_cache
def _model():
    import os

    from fastembed import TextEmbedding

    from cfo_agent.settings import get_settings

    name = get_settings().embedding_model or MODEL_NAME
    cache = os.environ.get("FASTEMBED_CACHE_PATH")
    return TextEmbedding(model_name=name, **({"cache_dir": cache} if cache else {}))


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
