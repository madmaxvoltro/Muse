"""Embedding helpers for the query API: text space (same model as content) and, through the
audio analyzer, CLAP's sound space. Both optional: without them a mood still works through its
audio-feature ranges."""

from __future__ import annotations

import logging
import os
import uuid

import httpx

from .embedder import get_embedder

log = logging.getLogger("muse.query.vectors")
AUDIO_URL = os.environ.get("MUSE_AUDIO_URL", "http://audio-analyzer:8004").rstrip("/")
NS = uuid.UUID("6f1b1b4a-2f2a-4b7b-9b0a-6a2c1d9e7f00")


def mood_point_id(name: str, space: str) -> str:
    return str(uuid.uuid5(NS, f"mood:{space}:{name.lower()}"))


async def text_vector(text: str) -> list[float] | None:
    emb = await get_embedder()
    if emb is None or not text:
        return None
    return list(emb.encode([text])[0])


async def sound_vector(text: str) -> list[float] | None:
    if not text:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{AUDIO_URL}/embed/text", json={"texts": [text]})
        return r.json()["vectors"][0] if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None


async def ensure_mood_vectors(qd) -> dict[str, int]:
    """Embed the mood presets once (text + sound) so the recommendation API can use them."""
    from muse_schema.moods import MOODS
    from qdrant_client.http.models import Distance, PointStruct, VectorParams

    done = {"text": 0, "audio": 0}
    for coll, space, dim, fn, field in (("mood_vectors", "text", 384, text_vector, "text"),
                                        ("mood_vectors_audio", "audio", 512, sound_vector, "sound")):
        try:
            if not qd.collection_exists(coll):
                qd.create_collection(coll, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
            have = {str(p.id) for p in qd.retrieve(coll, [mood_point_id(m, space) for m in MOODS])}
        except Exception:
            continue
        for name, d in MOODS.items():
            pid = mood_point_id(name, space)
            if pid in have or not d.get(field):
                continue
            v = await fn(d[field])
            if v is None:
                break  # embedder unavailable: try again next start
            qd.upsert(coll, [PointStruct(id=pid, vector=v, payload={"mood": name})])
            done[space] += 1
    return done
