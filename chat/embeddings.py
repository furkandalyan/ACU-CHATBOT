from __future__ import annotations

import hashlib
from functools import lru_cache

from django.conf import settings


EMBEDDING_DIMENSIONS = 384


def embedding_source_text(title: str, content: str) -> str:
    text = f"{title}\n\n{content}".strip()
    return text[:4000]


def embedding_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=2)
def get_embedding_model(model_name: str | None = None):
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name or settings.EMBEDDING_MODEL)


def embed_texts(texts: list[str], model_name: str | None = None) -> list[list[float]]:
    model = get_embedding_model(model_name)
    vectors = [list(vector) for vector in model.embed(texts)]
    for vector in vectors:
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Embedding dimension mismatch: expected {EMBEDDING_DIMENSIONS}, got {len(vector)}"
            )
    return vectors
