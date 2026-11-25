"""Audio analyzer service: a background loop that listens to tracks, plus a tiny API.

Loop (every MUSE_AUDIO_INTERVAL_SECONDS, MUSE_AUDIO_PER_CYCLE tracks per cycle so a CPU box
isn't pegged): queue library tracks (Mono) that have no analysis yet; for each pending item,
decode intro/body/outro (full tracks, streamed from Mono with a stream-only token) or the
30 s preview (catalog tracks, a fresh Deezer preview URL), compute features, embed the body
with CLAP, store features in Postgres (item_audio) and the vector + key features in Qdrant
(collection audio_items, same point ids as content_items).

API: GET /healthz, GET /status, POST /embed/text {"texts": [...]} -> CLAP text vectors (the
query API uses it for "search by sound" and taste portraits; the model lives only here).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from muse_schema.config import database_url
from muse_schema.storage import fetch_open, item_ids
from muse_schema.vectors import qdrant_client
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from muse_schema import content_point_id
from muse_schema.config import secret
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams

from . import features as F
from .embed import CLAP_SR, DIM, track_embedding
from .fetch import decode, segments

log = logging.getLogger("muse.audio")

AUDIO_COLLECTION = "audio_items"
QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
MONO_URL = os.environ.get("MUSE_MONO_URL", "").rstrip("/")
MONO_STREAM_TOKEN = secret("MUSE_MONO_STREAM_TOKEN", "") or ""
DEEZER_URL = os.environ.get("MUSE_DEEZER_URL", "https://api.deezer.com").rstrip("/")
NAVIDROME_URL = os.environ.get("MUSE_NAVIDROME_URL", "").rstrip("/")
NAVIDROME_USER = os.environ.get("MUSE_NAVIDROME_USER", "")
NAVIDROME_PASSWORD = secret("MUSE_NAVIDROME_PASSWORD", "") or ""
INTERVAL = int(os.environ.get("MUSE_AUDIO_INTERVAL_SECONDS", "60"))
PER_CYCLE = int(os.environ.get("MUSE_AUDIO_PER_CYCLE", "20"))
MAX_ATTEMPTS = 3
USE_CLAP = os.environ.get("MUSE_AUDIO_EMBEDDINGS", "1") == "1"


def ensure_collection(qd: QdrantClient) -> None:
    if not qd.collection_exists(AUDIO_COLLECTION):
        qd.create_collection(AUDIO_COLLECTION, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


async def enqueue_library(conn) -> int:
    sources = (["mono"] if MONO_URL and MONO_STREAM_TOKEN else []) + (["navidrome"] if NAVIDROME_URL and NAVIDROME_USER else [])
    if not sources:
        return 0
    # Events store item ids blind and metadata sealed (muse_schema.storage): open them here.
    rows = await fetch_open(
        conn,
        """
        SELECT DISTINCT ON (source, source_item_id) source, source_item_id, metadata FROM event
        WHERE item_type = 'track' AND action = 'cataloged' AND source = ANY($1::text[]) AND source_item_id IS NOT NULL
        ORDER BY source, source_item_id, "timestamp" DESC
        """,
        sources,
    )
    keys = [(r["source"], r["source_item_id"]) for r in rows if str((r["metadata"] or {}).get("in_library", True)).lower() != "false"]
    if not keys:
        return 0
    res = await conn.execute(
        "INSERT INTO item_audio (source, source_item_id) SELECT * FROM unnest($1::text[], $2::text[]) ON CONFLICT DO NOTHING",
        [k[0] for k in keys], [k[1] for k in keys],
    )
    return int(res.split()[-1])


async def resolve(conn, http: httpx.AsyncClient, source: str, sid: str) -> tuple[str, bool, float | None, dict] | None:
    """(url, is_preview, duration, metadata) or None when there is nothing to listen to."""
    md = next((r["metadata"] for r in await fetch_open(
        conn, "SELECT source, source_item_id, metadata FROM event WHERE source = $1 AND source_item_id = ANY($2::text[]) "
              "ORDER BY \"timestamp\" DESC LIMIT 50", source, item_ids(source, sid)) if r["metadata"]), {})
    if source == "mono" and MONO_URL and MONO_STREAM_TOKEN:
        return f"{MONO_URL}/api/tracks/{sid}/stream?t={MONO_STREAM_TOKEN}", False, md.get("duration_s"), md
    if source == "navidrome" and NAVIDROME_URL and NAVIDROME_USER:
        import hashlib
        import secrets
        from urllib.parse import urlencode

        salt = secrets.token_hex(6)
        q = urlencode({"id": sid, "u": NAVIDROME_USER, "t": hashlib.md5((NAVIDROME_PASSWORD + salt).encode()).hexdigest(), "s": salt,
                       "v": "1.16.1", "c": "muse-audio", "format": "raw"})
        return f"{NAVIDROME_URL}/rest/stream?{q}", False, md.get("duration_s"), md
    if source == "catalog" and md.get("deezer_id"):
        try:  # preview URLs are signed and expire: always ask for a fresh one
            r = await http.get(f"{DEEZER_URL}/track/{md['deezer_id']}")
            preview = (r.json() or {}).get("preview") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            preview = None
        if preview:
            return preview, True, 30.0, md
    return None


async def analyze_item(conn, qd: QdrantClient, http: httpx.AsyncClient, clap, source: str, sid: str, decoder=decode) -> str:
    target = await resolve(conn, http, source, sid)
    if target is None:
        await conn.execute("UPDATE item_audio SET status = 'unavailable', analyzed_at = now() WHERE source = $1 AND source_item_id = $2",
                           source, sid)
        return "unavailable"
    url, preview, duration, md = target
    parts: dict[str, dict] = {}
    body_wave: np.ndarray | None = None
    for name, (start, length) in segments(duration, preview).items():
        wave = await decoder(url, start, length, CLAP_SR)
        if name == "body":
            body_wave = wave
        parts[name] = await asyncio.to_thread(F.analyze, wave, CLAP_SR)
    if not parts.get("body"):
        raise RuntimeError("silent or too short")
    feats = F.summarize(parts["body"], parts.get("intro"), parts.get("outro"))
    vector = await asyncio.to_thread(track_embedding, clap, body_wave) if clap is not None and body_wave is not None else None
    if vector is not None:
        payload = {"source": source, "source_item_id": sid, "item_type": md.get("item_type", "track"),
                   "title": md.get("title", ""), "artist": md.get("artist", ""),
                   **{k: feats[k] for k in ("bpm", "energy", "camelot", "danceability", "acousticness", "brightness") if k in feats}}
        qd.upsert(AUDIO_COLLECTION, points=[PointStruct(id=content_point_id(source, sid), vector=vector, payload=payload)])
    await conn.execute(
        "UPDATE item_audio SET status = 'done', features = $3::text::jsonb, has_embedding = $4, model = $5, "
        "analyzed_at = now(), error = NULL WHERE source = $1 AND source_item_id = $2",
        source, sid, json.dumps(feats), vector is not None, getattr(clap, "name", None),
    )
    return "done"


async def run_cycle(pool, qd: QdrantClient, http: httpx.AsyncClient, clap, decoder=decode, limit: int = PER_CYCLE) -> dict:
    stats = {"queued": 0, "done": 0, "failed": 0, "unavailable": 0}
    async with pool.acquire() as conn:
        stats["queued"] = await enqueue_library(conn)
        # Library first (what you actually listen to), then catalog previews.
        rows = await conn.fetch(
            "SELECT source, source_item_id FROM item_audio WHERE status = 'pending' AND attempts < $1 "
            "ORDER BY (source = 'catalog'), attempts LIMIT $2",
            MAX_ATTEMPTS, limit,
        )
        for r in rows:
            try:
                stats[await analyze_item(conn, qd, http, clap, r["source"], r["source_item_id"], decoder)] += 1
            except Exception as e:  # one bad file must not stop the queue
                stats["failed"] += 1
                await conn.execute(
                    "UPDATE item_audio SET attempts = attempts + 1, error = $3, "
                    "status = CASE WHEN attempts + 1 >= $4 THEN 'failed' ELSE 'pending' END "
                    "WHERE source = $1 AND source_item_id = $2",
                    r["source"], r["source_item_id"], str(e)[:500], MAX_ATTEMPTS,
                )
    if any(stats.values()):
        log.info("audio cycle: %s", stats)
    return stats


# --- service --------------------------------------------------------------------------------------

state: dict[str, Any] = {"clap": None, "clap_error": None}


def load_clap():
    if not USE_CLAP:
        return None
    try:
        from .embed import Clap

        return Clap()
    except Exception as e:  # no torch/transformers, or the model can't be downloaded
        state["clap_error"] = str(e)[:300]
        log.warning("audio embeddings disabled: %s", e)
        return None


async def _loop() -> None:
    pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=3)
    qd = qdrant_client(QDRANT_URL)
    ensure_collection(qd)
    state["clap"] = await asyncio.to_thread(load_clap)
    state["pool"] = pool
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "MuseAudio/0.1"}) as http:
        while True:
            try:
                await run_cycle(pool, qd, http, state["clap"])
            except Exception:
                log.exception("audio cycle failed")
            await asyncio.sleep(INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    task = asyncio.create_task(_loop())
    yield
    task.cancel()


app = FastAPI(title="Muse Audio Analyzer", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict:
    counts = {}
    if state.get("pool") is not None:
        async with state["pool"].acquire() as conn:
            counts = {r["status"]: r["n"] for r in await conn.fetch("SELECT status, count(*) AS n FROM item_audio GROUP BY 1")}
    clap = state.get("clap")
    return {"items": counts, "embeddings": clap is not None, "model": getattr(clap, "name", None),
            "device": getattr(clap, "device", None), "embeddings_error": state.get("clap_error")}


class TextIn(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=256)


@app.post("/embed/text")
async def embed_text(body: TextIn) -> dict:
    clap = state.get("clap")
    if clap is None:
        raise HTTPException(503, "audio embeddings are not available on this server")
    return {"model": clap.name, "vectors": await asyncio.to_thread(clap.text, [t[:300] for t in body.texts])}
