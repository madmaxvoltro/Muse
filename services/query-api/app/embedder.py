"""Query embeddings for semantic search. Must be the same model the embedding worker used
for content (MUSE_EMBEDDING_MODEL), or similarities are meaningless. Loaded lazily; if
sentence-transformers isn't installed (MUSE_QUERY_EMBEDDER=none, or a slim image), search
falls back to keyword matching only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

log = logging.getLogger("muse.query.embedder")
MODEL_NAME = os.environ.get("MUSE_EMBEDDING_MODEL", "all-MiniLM-L6-v2")


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class _SentenceTransformerEmbedder:
    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(MODEL_NAME)

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self.model.encode(texts, normalize_embeddings=True)]


_embedder: Any = None
_tried = False
_lock = asyncio.Lock()


def set_embedder(e: Embedder | None) -> None:
    """For tests, or to share a model in-process."""
    global _embedder, _tried
    _embedder, _tried = e, True


async def get_embedder() -> Embedder | None:
    global _embedder, _tried
    if _tried:
        return _embedder
    async with _lock:
        if not _tried:
            if os.environ.get("MUSE_QUERY_EMBEDDER", "auto") != "none":
                try:
                    _embedder = await asyncio.to_thread(_SentenceTransformerEmbedder)
                    log.info("semantic search enabled (%s)", MODEL_NAME)
                except ImportError:
                    log.warning("sentence-transformers not installed: keyword search only")
            _tried = True
    return _embedder
