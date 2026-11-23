"""Vector worker (the multi-space engine's worker; worker.py is the taste engine's) — computes content embeddings for items and time-decayed
taste vectors for users, on a fixed interval. Runs as a standalone loop rather
than a request-driven service; there's no reason for this to be a web service.

Design (see docs/architecture.md):
- Content embedding: local sentence-transformers model on item metadata text.
  Deliberately not calling any external embedding API — privacy requirement.
- Taste vector: weighted average of the content vectors of everything a user
  interacted with, weight = action_weight * exp(-age_days / DECAY_HALFLIFE_DAYS).
  Computed per item_type plus one combined cross-domain vector, so e.g. music
  taste never pollutes film recommendations.
- Taste is per person, not per id: events of every linked identity (user_alias) count, and
  the vector is stored under the canonical id (see shared/muse_schema/identity.py).
- Facts (profile_fact, written via the query API / MCP) are embedded and added like items,
  weight = sentiment * confidence * FACT_WEIGHT, so "she loves slow Dutch songs" moves the
  vector even before a single matching play. Domain-specific facts shape that domain (and
  can create its vector from nothing: an interview answers cold start); 'general' facts
  shape every domain.
"""

import asyncio
import json
import logging
import math
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import asyncpg
from muse_schema import canonical_map, content_point_id, identity_group, taste_point_id
from muse_schema import context as ctxmod
from muse_schema.config import database_url
from muse_schema.storage import open_event
from muse_schema.vectors import qdrant_client
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.embedding_worker")

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
MODEL_NAME = os.environ.get("MUSE_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
RUN_INTERVAL_SECONDS = int(os.environ.get("MUSE_EMBEDDING_INTERVAL_SECONDS", "300"))
DEEP_INTERVAL_SECONDS = int(os.environ.get("MUSE_DEEP_TASTE_INTERVAL_SECONDS", "3600"))
DECAY_HALFLIFE_DAYS = float(os.environ.get("MUSE_TASTE_DECAY_HALFLIFE_DAYS", "60"))
TASTE_LOOKBACK_DAYS = int(os.environ.get("MUSE_TASTE_LOOKBACK_DAYS", "730"))
# A stated preference counts like this many favourites' worth of weight at full confidence.
FACT_WEIGHT = float(os.environ.get("MUSE_FACT_WEIGHT", "2.0"))
FACT_KINDS = ("likes", "dislikes", "avoid")
CONTEXT_MIN_EVENTS = int(os.environ.get("MUSE_CONTEXT_MIN_EVENTS", "15"))

CONTENT_COLLECTION = "content_items"
TASTE_COLLECTION = "taste_vectors"
VECTOR_SIZE = 384  # all-MiniLM-L6-v2 output dim


def item_text(metadata: dict) -> str:
    """Builds the text blob to embed for an item. v1: metadata fields only.
    TODO (see docs/architecture.md): incorporate transcripts/subtitles where available.
    """
    parts = [
        metadata.get("title", ""),
        metadata.get("artist", "") if metadata.get("artist", "") not in metadata.get("title", "") else "",
        metadata.get("album", ""),
        metadata.get("description", ""),
        " ".join(metadata.get("genres", []) or []),
        " ".join(metadata.get("tags", []) or []),
        metadata.get("mood", ""),
        metadata.get("query", ""),  # search_query items
    ]
    return " ".join(str(p) for p in parts if p).strip()


def fact_text(fact: dict) -> str:
    subject = fact.get("subject") or ""
    statement = fact.get("statement") or ""
    return f"{subject}. {statement}" if subject and subject.casefold() not in statement.casefold() else statement


def ensure_collections(client: QdrantClient) -> None:
    for name in (CONTENT_COLLECTION, TASTE_COLLECTION):
        if not client.collection_exists(name):
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            logger.info("created Qdrant collection %s", name)


async def embed_new_content(
    pool: asyncpg.Pool, qdrant: QdrantClient, model: Any
) -> tuple[set[str], datetime, datetime]:
    """Embeds items from events received since the last run. Returns the user_ids touched
    (so their taste vectors can be recomputed) and the old and new watermark; run_once saves
    the new one only after the taste vectors are done, so a failure retries the whole batch.
    Progress is tracked by ingested_at, not "timestamp": imported history carries old
    timestamps and would otherwise be skipped.
    """
    async with pool.acquire() as conn:
        state = await conn.fetchrow("SELECT last_processed_at FROM vector_worker_state")
        since = state["last_processed_at"]

        # The newest event *with metadata* per item: plays and skips often carry none, and
        # picking one of those would leave the item un-embedded forever.
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (source, source_item_id)
                source, source_item_id, item_type, metadata, user_id, "timestamp"
            FROM event
            WHERE ingested_at > $1 AND source_item_id IS NOT NULL AND metadata <> '{}'::jsonb
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
        points: list[PointStruct] = []

        for row in rows:
            row = open_event(row)  # real item id and metadata (sealed at rest, see muse_schema.storage)
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

        return touched_users, since, max_ts


def decay_weight(event_ts: datetime, now: datetime) -> float:
    age_days = max((now - event_ts).total_seconds() / 86400.0, 0.0)
    return math.exp(-age_days / DECAY_HALFLIFE_DAYS)


async def recompute_taste_vectors(
    pool: asyncpg.Pool, qdrant: QdrantClient, user_ids: set[str], model: Any = None
) -> None:
    """user_ids may contain aliases: each person is recomputed once, under the canonical id,
    from the events of all their linked ids plus their active facts."""
    if not user_ids:
        return

    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        canonicals = sorted(set((await canonical_map(conn, list(user_ids))).values()))
        for user_id in canonicals:
            canonical, group = await identity_group(conn, user_id)
            rows = await conn.fetch(
                """
                SELECT source, source_item_id, item_type, action_weight, "timestamp", device_context, metadata
                FROM event
                WHERE user_id = ANY($1::uuid[])
                  AND source_item_id IS NOT NULL
                  AND "timestamp" > now() - make_interval(days => $2::int)
                """,
                group,
                TASTE_LOOKBACK_DAYS,
            )
            facts = await conn.fetch(
                """
                SELECT id, kind, domain, statement, subject, sentiment, confidence FROM profile_fact
                WHERE user_id = $1 AND retracted_at IS NULL AND kind = ANY($2::text[]) AND sentiment <> 0
                """,
                canonical,
                list(FACT_KINDS),
            )
            if not rows and not facts:
                continue

            # Group weighted content-vector lookups per item_type, plus one combined bucket.
            per_type: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
            for row in map(open_event, rows):
                w = row["action_weight"] * decay_weight(row["timestamp"], now)
                if w == 0:
                    continue
                per_type[row["item_type"]].append((row["source"], row["source_item_id"], w))
                per_type["_combined"].append((row["source"], row["source_item_id"], w))
                # Context tastes: "track@night", "movie@weekend", "track@car" ...
                dc = row["device_context"]
                dc = json.loads(dc) if isinstance(dc, str) else dc
                for b in ctxmod.buckets(dc, row["timestamp"]):
                    per_type[ctxmod.bucket_key(row["item_type"], b)].append((row["source"], row["source_item_id"], w))
            for bucket in [b for b in per_type if "@" in b and len(per_type[b]) < CONTEXT_MIN_EVENTS]:
                del per_type[bucket]  # too little to say anything about that situation

            # Facts: (vector, weight) per bucket. 'general' facts go into every bucket.
            fact_entries: dict[str, list[tuple[list[float], float]]] = defaultdict(list)
            if facts and model is not None:
                texts = [fact_text(dict(f)) for f in facts]
                vectors = model.encode(texts, normalize_embeddings=True)
                general: list[tuple[list[float], float]] = []
                for f, vec in zip(facts, vectors):
                    w = float(f["sentiment"]) * float(f["confidence"]) * FACT_WEIGHT
                    entry = (list(map(float, vec)), w)
                    if f["domain"] == "general":
                        general.append(entry)
                    else:
                        fact_entries[f["domain"]].append(entry)
                    fact_entries["_combined"].append(entry)
                for bucket in set(per_type) | set(fact_entries):
                    if bucket != "_combined" and "@" not in bucket:
                        fact_entries[bucket].extend(general)

            for item_type in set(per_type) | set(fact_entries):
                entries = per_type.get(item_type, [])
                point_ids = [content_point_id(s, sid) for s, sid, _ in entries]
                fetched = (
                    qdrant.retrieve(collection_name=CONTENT_COLLECTION, ids=point_ids, with_vectors=True)
                    if point_ids else []
                )
                vectors_by_id = {str(p.id): p.vector for p in fetched}

                weighted_sum = [0.0] * VECTOR_SIZE
                total_weight = 0.0
                for (s, sid, w), pid in zip(entries, point_ids):
                    vec = vectors_by_id.get(pid)
                    if vec is None:
                        continue  # content not embedded yet
                    for i, v in enumerate(vec):
                        weighted_sum[i] += v * w
                    total_weight += abs(w)
                for vec, w in fact_entries.get(item_type, []):
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
                            id=taste_point_id(str(canonical), item_type),
                            vector=taste_vector,
                            payload={
                                "user_id": str(canonical),
                                "item_type": item_type,
                                "events": len(entries),
                                "facts": len(fact_entries.get(item_type, [])),
                            },
                        )
                    ],
                )
            logger.info(
                "recomputed taste vectors for user=%s (%d ids, %d facts, %d buckets)",
                canonical, len(group), len(facts), len(set(per_type) | set(fact_entries)),
            )


async def drain_dirty(pool: asyncpg.Pool) -> set[str]:
    """Users the query API marked (new/retracted fact, identities linked)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("DELETE FROM taste_dirty RETURNING user_id")
    return {str(r["user_id"]) for r in rows}


async def run_once(pool: asyncpg.Pool, qdrant: QdrantClient, model: Any) -> None:
    touched_users, since, max_ts = await embed_new_content(pool, qdrant, model)
    dirty = await drain_dirty(pool)
    try:
        await recompute_taste_vectors(pool, qdrant, touched_users | dirty, model)
    except Exception:
        if dirty:  # put them back so the next cycle retries
            async with pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO taste_dirty (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    [(uuid.UUID(u),) for u in dirty],
                )
        raise
    if max_ts > since:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE vector_worker_state SET last_processed_at = $1", max_ts)


async def maybe_deep_taste(pool: asyncpg.Pool, qdrant: QdrantClient, state: dict) -> dict | None:
    """Hourly: facets, audio taste, preference models, behaviour embeddings (deep_taste.py),
    for people with activity since the last run."""
    import deep_taste

    now = time.monotonic()
    if state.get("last_deep") is not None and now - state["last_deep"] < DEEP_INTERVAL_SECONDS:
        return None
    since = state.get("deep_since", datetime(1970, 1, 1, tzinfo=timezone.utc))
    started = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT COALESCE(a.user_id, e.user_id) AS person FROM event e LEFT JOIN user_alias a ON a.alias_id = e.user_id "
            "WHERE e.ingested_at > $1 AND e.action <> 'cataloged'", since)
    if state.get("descriptors") is None:
        state["descriptors"] = await deep_taste.descriptor_vectors()
    result = await deep_taste.run(pool, qdrant, [str(r["person"]) for r in rows], state.get("descriptors"))
    import ranker_training

    async with pool.acquire() as conn:
        try:
            result["impressions_labelled"] = await ranker_training.label_outcomes(conn)
            result["ranker"] = await ranker_training.train(conn)
        except asyncpg.UndefinedTableError:
            pass  # database without migration 008
    state["last_deep"], state["deep_since"] = now, started
    return result


async def _json_codecs(conn: asyncpg.Connection) -> None:
    # asyncpg returns json/jsonb as text by default; event.metadata is read as a dict.
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def main() -> None:
    from sentence_transformers import SentenceTransformer  # heavy import, only for the real loop

    logger.info("loading embedding model %s", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)

    qdrant = qdrant_client(QDRANT_URL)
    ensure_collections(qdrant)

    pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=5, init=_json_codecs)

    logger.info("embedding worker started, interval=%ds", RUN_INTERVAL_SECONDS)
    deep_state: dict = {}
    while True:
        start = time.monotonic()
        try:
            await run_once(pool, qdrant, model)
        except Exception:
            logger.exception("embedding worker cycle failed")
        try:
            await maybe_deep_taste(pool, qdrant, deep_state)
        except Exception:
            logger.exception("deep taste cycle failed")
        elapsed = time.monotonic() - start
        await asyncio.sleep(max(RUN_INTERVAL_SECONDS - elapsed, 5))


if __name__ == "__main__":
    asyncio.run(main())
