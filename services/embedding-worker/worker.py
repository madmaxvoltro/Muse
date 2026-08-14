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


def item_text(metadata: dict) -> str:
    """Builds the text blob to embed for an item. v1: metadata fields only.
    TODO (see docs/architecture.md): incorporate transcripts/subtitles where available.
    """
    parts = [
        metadata.get("title", ""),
        metadata.get("description", ""),
        " ".join(metadata.get("genres", []) or []),
        metadata.get("query", ""),  # search_query items
    ]
    return " ".join(p for p in parts if p).strip()


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
) -> set[str]:
    """Embeds items from events written since the last run. Returns the set of
    user_ids touched, so their taste vectors can be recomputed afterwards.
    """
    async with pool.acquire() as conn:
        state = await conn.fetchrow("SELECT last_processed_at FROM embedding_worker_state")
        since = state["last_processed_at"]

        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (source, source_item_id)
                source, source_item_id, item_type, metadata, user_id, "timestamp"
            FROM event
            WHERE "timestamp" > $1 AND source_item_id IS NOT NULL
            ORDER BY source, source_item_id, "timestamp" DESC
            """,
            since,
        )

        touched_users: set[str] = set()
        points: list[PointStruct] = []
        max_ts = since

        for row in rows:
            touched_users.add(str(row["user_id"]))
            max_ts = max(max_ts, row["timestamp"])

            text = item_text(dict(row["metadata"]) if row["metadata"] else {})
            if not text:
                continue  # nothing to embed this item on yet

            vector = model.encode(text, normalize_embeddings=True).tolist()
            points.append(
                PointStruct(
                    id=content_point_id(row["source"], row["source_item_id"]),
                    vector=vector,
                    payload={
                        "source": row["source"],
                        "source_item_id": row["source_item_id"],
                        "item_type": row["item_type"],
                        "title": (row["metadata"] or {}).get("title", ""),
                        "genres": (row["metadata"] or {}).get("genres", []) or [],
                    },
                )
            )

        if points:
            qdrant.upsert(collection_name=CONTENT_COLLECTION, points=points)
            logger.info("upserted %d content embeddings", len(points))

        if max_ts > since:
            await conn.execute(
                "UPDATE embedding_worker_state SET last_processed_at = $1", max_ts
            )

        return touched_users


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
                SELECT source, source_item_id, item_type, action_weight, "timestamp"
                FROM event
                WHERE user_id = $1
                  AND source_item_id IS NOT NULL
                  AND "timestamp" > now() - ($2 || ' days')::interval
                """,
                uuid.UUID(user_id),
                TASTE_LOOKBACK_DAYS,
            )
            if not rows:
                continue

            # Group weighted content-vector lookups per item_type, plus one combined bucket.
            per_type: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
            for row in rows:
                w = row["action_weight"] * decay_weight(row["timestamp"], now)
                if w == 0:
                    continue
                per_type[row["item_type"]].append((row["source"], row["source_item_id"], w))
                per_type["_combined"].append((row["source"], row["source_item_id"], w))

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
    touched_users = await embed_new_content(pool, qdrant, model)
    await recompute_taste_vectors(pool, qdrant, touched_users)


async def main() -> None:
    logger.info("loading embedding model %s", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)

    qdrant = QdrantClient(url=QDRANT_URL)
    ensure_collections(qdrant)

    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)

    logger.info("embedding worker started, interval=%ds", RUN_INTERVAL_SECONDS)
    while True:
        start = time.monotonic()
        try:
            await run_once(pool, qdrant, model)
        except Exception:
            logger.exception("embedding worker cycle failed")
        elapsed = time.monotonic() - start
        await asyncio.sleep(max(RUN_INTERVAL_SECONDS - elapsed, 5))


if __name__ == "__main__":
    asyncio.run(main())
