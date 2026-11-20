"""Embedding worker — computes content embeddings for items and time-decayed
taste vectors for users, on a fixed interval. Runs as a standalone loop rather
than a request-driven service; there's no reason for this to be a web service.

Design (see docs/architecture.md):
- Content embedding: local sentence-transformers model on item metadata text.
  Deliberately not calling any external embedding API — privacy requirement.
- Taste vector: weighted average of the content vectors of everything a user
  interacted with, weight = action_weight * exp(-age_days / DECAY_HALFLIFE_DAYS).
  Computed per item_type plus one combined cross-domain vector, so e.g. music
  taste never pollutes film recommendations.
"""

import asyncio
import json
import threading
import logging
import math
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import asyncpg
from muse_schema import content_point_id, taste_point_id
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer

from content import build_payload, collapse_events, item_text, merge_metadata

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.embedding_worker")

DATABASE_URL = os.environ["MUSE_DATABASE_URL"]
QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
MODEL_NAME = os.environ.get("MUSE_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
RUN_INTERVAL_SECONDS = int(os.environ.get("MUSE_EMBEDDING_INTERVAL_SECONDS", "300"))
DECAY_HALFLIFE_DAYS = float(os.environ.get("MUSE_TASTE_DECAY_HALFLIFE_DAYS", "60"))
TASTE_LOOKBACK_DAYS = int(os.environ.get("MUSE_TASTE_LOOKBACK_DAYS", "730"))

CONTENT_COLLECTION = "content_items"
TASTE_COLLECTION = "taste_vectors"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 output dim
EMBED_PORT = int(os.environ.get("MUSE_EMBED_PORT", "8002"))  # 0 disables the /embed endpoint

# One model instance serves the worker loop and the /embed endpoint; encode() calls are
# serialised so they never run concurrently on the same model.
_model_lock = threading.Lock()


def encode(model: SentenceTransformer, texts: list[str]) -> list[list[float]]:
    with _model_lock:
        return model.encode(texts, normalize_embeddings=True, batch_size=32).tolist()


def ensure_collections(client: QdrantClient) -> None:
    for name in (CONTENT_COLLECTION, TASTE_COLLECTION):
        if not client.collection_exists(name):
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            logger.info("created Qdrant collection %s", name)


async def embed_new_content(
    pool: asyncpg.Pool, qdrant: QdrantClient, model: SentenceTransformer
) -> tuple[set[str], datetime, datetime]:
    """Embeds items from events received since the last run. Returns the user_ids touched
    (so their taste vectors can be recomputed) and the old and new watermark; run_once saves
    the new one only after the taste vectors are done, so a failure retries the whole batch.
    Progress is tracked by ingested_at, not "timestamp": imported history carries old
    timestamps and would otherwise be skipped.
    """
    async with pool.acquire() as conn:
        state = await conn.fetchrow("SELECT last_processed_at FROM embedding_worker_state")
        since = state["last_processed_at"]

        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (source, source_item_id)
                source, source_item_id, item_type, metadata, user_id, "timestamp"
            FROM event
            WHERE ingested_at > $1 AND source_item_id IS NOT NULL
            ORDER BY source, source_item_id, "timestamp" DESC
            """,
            since,
        )
        # Every user with new events, not only the one whose row DISTINCT ON kept per item.
        users = await conn.fetch(
            "SELECT user_id, max(ingested_at) AS last FROM event WHERE ingested_at > $1 GROUP BY user_id", since
        )
        touched_users: set[str] = {str(u["user_id"]) for u in users}
        max_ts = max([since, *(u["last"] for u in users)])

    # Merge with what Qdrant already knows about each item, so an event with thin metadata
    # (a Vesper detail view) never erases directors, ratings or ids from a catalog event.
    ids = [content_point_id(r["source"], r["source_item_id"]) for r in rows]
    previous: dict[str, dict] = {}
    for i in range(0, len(ids), 256):
        for p in qdrant.retrieve(collection_name=CONTENT_COLLECTION, ids=ids[i : i + 256], with_payload=True):
            previous[str(p.id)] = p.payload or {}

    now_iso = datetime.now(timezone.utc).isoformat()
    pending: list[tuple[str, dict, str]] = []
    for row, pid in zip(rows, ids):
        prev = previous.get(pid)
        metadata = merge_metadata((prev or {}).get("metadata"), dict(row["metadata"]) if row["metadata"] else {})
        text = item_text(metadata)
        if not text:
            continue  # nothing to embed this item on yet
        payload = build_payload(row["source"], row["source_item_id"], row["item_type"], metadata, prev, now_iso)
        pending.append((pid, payload, text))

    points: list[PointStruct] = []
    for i in range(0, len(pending), 256):
        batch = pending[i : i + 256]
        vectors = await asyncio.to_thread(encode, model, [t for _, _, t in batch])
        points.extend(PointStruct(id=pid, vector=v, payload=payload) for (pid, payload, _), v in zip(batch, vectors))

    if points:
        for i in range(0, len(points), 256):
            qdrant.upsert(collection_name=CONTENT_COLLECTION, points=points[i : i + 256])
        logger.info("upserted %d content embeddings", len(points))

    return touched_users, since, max_ts


def decay_weight(event_ts: datetime, now: datetime) -> float:
    age_days = max((now - event_ts).total_seconds() / 86400.0, 0.0)
    return math.exp(-age_days / DECAY_HALFLIFE_DAYS)


async def recompute_taste_vectors(
    pool: asyncpg.Pool, qdrant: QdrantClient, user_ids: set[str]
) -> None:
    if not user_ids:
        return

    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        for user_id in user_ids:
            rows = await conn.fetch(
                """
                SELECT source, source_item_id, item_type, action, action_weight, "timestamp",
                       coalesce((metadata->>'co_viewer')::boolean, false) AS co_viewer
                FROM event
                WHERE user_id = $1
                  AND source_item_id IS NOT NULL
                  AND action_weight <> 0
                  AND "timestamp" > now() - make_interval(days => $2::int)
                """,
                uuid.UUID(user_id),
                TASTE_LOOKBACK_DAYS,
            )
            if not rows:
                continue

            # Group weighted content-vector lookups per item_type, plus one combined bucket.
            per_type: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
            for source, sid, item_type, weight, ts in collapse_events([dict(r) for r in rows]):
                w = weight * decay_weight(ts, now)
                if w == 0:
                    continue
                per_type[item_type].append((source, sid, w))
                per_type["_combined"].append((source, sid, w))

            for item_type, entries in per_type.items():
                point_ids = [content_point_id(s, sid) for s, sid, _ in entries]
                fetched = qdrant.retrieve(collection_name=CONTENT_COLLECTION, ids=point_ids, with_vectors=True)
                vectors_by_id = {p.id: p.vector for p in fetched}

                weighted_sum = [0.0] * VECTOR_SIZE
                total_weight = 0.0
                for (s, sid, w), pid in zip(entries, point_ids):
                    vec = vectors_by_id.get(pid)
                    if vec is None:
                        continue  # content not embedded yet
                    for i, v in enumerate(vec):
                        weighted_sum[i] += v * w
                    total_weight += abs(w)

                if total_weight == 0:
                    continue
                taste_vector = [x / total_weight for x in weighted_sum]

                qdrant.upsert(
                    collection_name=TASTE_COLLECTION,
                    points=[
                        PointStruct(
                            id=taste_point_id(user_id, item_type),
                            vector=taste_vector,
                            payload={"user_id": user_id, "item_type": item_type},
                        )
                    ],
                )
            logger.info("recomputed taste vectors for user=%s (%d item_types)", user_id, len(per_type))


async def run_once(pool: asyncpg.Pool, qdrant: QdrantClient, model: SentenceTransformer) -> None:
    touched_users, since, max_ts = await embed_new_content(pool, qdrant, model)
    await recompute_taste_vectors(pool, qdrant, touched_users)
    if max_ts > since:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE embedding_worker_state SET last_processed_at = $1", max_ts)


async def _json_codecs(conn: asyncpg.Connection) -> None:
    # asyncpg returns json/jsonb as text by default; event.metadata is read as a dict.
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

def embed_app(model: SentenceTransformer):
    """POST /embed {"texts": [...]} -> {"vectors": [[...]]}. Used by the recommendation API
    for semantic search and mood prompts, so queries are embedded with exactly the model the
    content was embedded with, and the model is loaded once. Internal network only.
    """
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field

    app = FastAPI(title="Muse Embedding Worker", version="0.2.0")

    class EmbedRequest(BaseModel):
        texts: list[str] = Field(..., min_length=1, max_length=64)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "model": MODEL_NAME, "dim": VECTOR_SIZE}

    @app.post("/embed")
    async def embed(req: EmbedRequest) -> dict:
        texts = [t.strip()[:2000] for t in req.texts]
        if not all(texts):
            raise HTTPException(status_code=400, detail="texts must be non-empty")
        vectors = await asyncio.to_thread(encode, model, texts)
        return {"model": MODEL_NAME, "dim": VECTOR_SIZE, "vectors": vectors}

    return app


async def serve_embed(model: SentenceTransformer) -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(embed_app(model), host="0.0.0.0", port=EMBED_PORT, log_level="warning"))
    await server.serve()


async def worker_loop(pool: asyncpg.Pool, qdrant: QdrantClient, model: SentenceTransformer) -> None:
    logger.info("embedding worker started, interval=%ds", RUN_INTERVAL_SECONDS)
    while True:
        start = time.monotonic()
        try:
            await run_once(pool, qdrant, model)
        except Exception:
            logger.exception("embedding worker cycle failed")
        elapsed = time.monotonic() - start
        await asyncio.sleep(max(RUN_INTERVAL_SECONDS - elapsed, 5))


async def main() -> None:
    logger.info("loading embedding model %s", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)

    qdrant = QdrantClient(url=QDRANT_URL)
    ensure_collections(qdrant)

    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5, init=_json_codecs)

    tasks = [worker_loop(pool, qdrant, model)]
    if EMBED_PORT:
        logger.info("embed endpoint on :%d", EMBED_PORT)
        tasks.append(serve_embed(model))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
