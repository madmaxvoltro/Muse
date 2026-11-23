"""One way to reach Qdrant from every service, with the vault applied to payloads.

`qdrant_client()` connects with MUSE_QDRANT_URL and the API key from MUSE_QDRANT_API_KEY(_FILE)
(services get a read-only or a read-write key, see docker-compose.yml), and wraps the client so
that payloads of the multi-space collections (content_items, taste_vectors, audio_items, ...)
are sealed on write and opened on read, exactly like the taste engine seals `muse_items`
(muse_taste.store). Only the fields Qdrant must filter on stay readable: source, item_type,
user_id and added_at; titles, genres, people and everything else are ciphertext on disk.

With MUSE_ENCRYPTION=off (development, tests that opt out) payloads pass through untouched, and
points written before encryption was turned on are read as they are.
"""

from __future__ import annotations

import os
from typing import Any

from .config import secret
from .crypto import P_PAYLOAD, get_vault

PLAIN_KEYS = frozenset({"source", "item_type", "user_id", "added_at"})
# The taste engine's collection seals its own payloads (muse_taste.store.payload_for).
SELF_SEALED = frozenset({"muse_items"})
SEALED_FIELD = "_s"


def seal_payload(payload: dict | None, vault: Any = None) -> dict | None:
    if not payload:
        return payload
    vault = vault or get_vault()
    if not vault.enabled or SEALED_FIELD in payload:
        return payload
    plain = {k: v for k, v in payload.items() if k in PLAIN_KEYS}
    rest = {k: v for k, v in payload.items() if k not in PLAIN_KEYS}
    if rest:
        plain[SEALED_FIELD] = vault.seal_json(rest, P_PAYLOAD)
    return plain


def open_payload(payload: dict | None, vault: Any = None) -> dict | None:
    if not payload or SEALED_FIELD not in payload:
        return payload
    vault = vault or get_vault()
    out = {k: v for k, v in payload.items() if k != SEALED_FIELD}
    opened = vault.open_json(payload[SEALED_FIELD], P_PAYLOAD)
    if isinstance(opened, dict):
        out.update(opened)
    return out


def _open_points(points: Any) -> Any:
    for p in points or []:
        if getattr(p, "payload", None):
            p.payload = open_payload(p.payload)
    return points


class SealedQdrant:
    """A QdrantClient whose payloads are sealed at rest (see module docstring). Every other
    method is passed through unchanged."""

    def __init__(self, client: Any):
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    @staticmethod
    def _sealed(collection_name: str) -> bool:
        return collection_name not in SELF_SEALED

    def upsert(self, collection_name: str, points: Any, **kw: Any) -> Any:
        if self._sealed(collection_name) and isinstance(points, list):
            for p in points:
                if getattr(p, "payload", None):
                    p.payload = seal_payload(p.payload)
        return self._client.upsert(collection_name=collection_name, points=points, **kw)

    def set_payload(self, collection_name: str, payload: dict, **kw: Any) -> Any:
        if self._sealed(collection_name) and get_vault().enabled and set(payload) - PLAIN_KEYS:
            raise ValueError("set_payload on a sealed collection would store plaintext; upsert the whole point instead")
        return self._client.set_payload(collection_name=collection_name, payload=payload, **kw)

    def retrieve(self, collection_name: str, *a: Any, **kw: Any) -> Any:
        res = self._client.retrieve(collection_name, *a, **kw)
        return _open_points(res) if self._sealed(collection_name) else res

    def query_points(self, collection_name: str, *a: Any, **kw: Any) -> Any:
        res = self._client.query_points(collection_name, *a, **kw)
        if self._sealed(collection_name):
            _open_points(res.points)
        return res

    def search(self, collection_name: str, *a: Any, **kw: Any) -> Any:
        res = self._client.search(collection_name, *a, **kw)
        return _open_points(res) if self._sealed(collection_name) else res

    def scroll(self, collection_name: str, *a: Any, **kw: Any) -> Any:
        points, offset = self._client.scroll(collection_name, *a, **kw)
        if self._sealed(collection_name):
            _open_points(points)
        return points, offset


def qdrant_client(url: str | None = None) -> SealedQdrant:
    from qdrant_client import QdrantClient

    url = url or os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
    raw = QdrantClient(":memory:") if url == ":memory:" else QdrantClient(url=url, api_key=secret("MUSE_QDRANT_API_KEY") or None)
    return SealedQdrant(raw)
