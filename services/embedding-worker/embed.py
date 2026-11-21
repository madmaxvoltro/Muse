"""Vector store sync: embed canonical documents that changed, push payload-only changes, drop
orphans. Changing the embedding model changes every doc_hash (the model name is part of it),
so a model switch re-embeds everything automatically; a dimension change also recreates the
collection."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import asyncpg
from muse_schema import canon_point_id
from muse_taste.store import BOOL_INDEXES, FLOAT_INDEXES, ITEMS_COLLECTION, KEYWORD_INDEXES, payload_for
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logger = logging.getLogger("muse.worker.embed")

BATCH = 64


def ensure_collection(qdrant: QdrantClient, dim: int) -> bool:
    """Returns True if the collection was (re)created."""
    if qdrant.collection_exists(ITEMS_COLLECTION):
        info = qdrant.get_collection(ITEMS_COLLECTION)
        vec = info.config.params.vectors
        size = vec.size if hasattr(vec, "size") else None
        if size == dim:
            return False
        logger.warning("embedding dimension changed (%s -> %s): recreating %s", size, dim, ITEMS_COLLECTION)
        qdrant.delete_collection(ITEMS_COLLECTION)
    qdrant.create_collection(ITEMS_COLLECTION, vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE))
    for f in KEYWORD_INDEXES:
        qdrant.create_payload_index(ITEMS_COLLECTION, f, qm.PayloadSchemaType.KEYWORD)
    for f in BOOL_INDEXES:
        qdrant.create_payload_index(ITEMS_COLLECTION, f, qm.PayloadSchemaType.BOOL)
    for f in FLOAT_INDEXES:
        qdrant.create_payload_index(ITEMS_COLLECTION, f, qm.PayloadSchemaType.FLOAT)
    logger.info("created Qdrant collection %s (dim=%d)", ITEMS_COLLECTION, dim)
    return True


CANON_COLS = "canon_key, item_type, domain, title, year, tags, collection_id, vote_average, vote_count, popularity, available, library_refs, added_at, document, doc_hash, payload_hash"


async def sync_vectors(pool: asyncpg.Pool, qdrant: QdrantClient, embedder, time_budget_s: float = 240.0) -> int:
    start = time.monotonic()
    total = 0
    while time.monotonic() - start < time_budget_s:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {CANON_COLS} FROM canon_item WHERE embedded_hash IS DISTINCT FROM doc_hash ORDER BY available DESC, canon_key LIMIT $1",
                BATCH,
            )
        if not rows:
            break
        docs = [r["document"] for r in rows]
        vecs = await asyncio.to_thread(embedder.embed, docs)
        points = [
            qm.PointStruct(id=canon_point_id(r["canon_key"]), vector=v.tolist(), payload=payload_for(_row(r)))
            for r, v in zip(rows, vecs)
        ]
        qdrant.upsert(ITEMS_COLLECTION, points=points)
        async with pool.acquire() as conn:
            await conn.executemany(
                "UPDATE canon_item SET embedded_hash = $2, synced_payload_hash = $3 WHERE canon_key = $1",
                [(r["canon_key"], r["doc_hash"], r["payload_hash"]) for r in rows],
            )
        total += len(rows)
    # payload-only changes (availability, ratings) without re-embedding
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {CANON_COLS} FROM canon_item WHERE embedded_hash = doc_hash AND synced_payload_hash IS DISTINCT FROM payload_hash LIMIT 2000"
        )
    for r in rows:
        qdrant.set_payload(ITEMS_COLLECTION, payload=payload_for(_row(r)), points=[canon_point_id(r["canon_key"])])
    if rows:
        async with pool.acquire() as conn:
            await conn.executemany(
                "UPDATE canon_item SET synced_payload_hash = $2 WHERE canon_key = $1", [(r["canon_key"], r["payload_hash"]) for r in rows]
            )
    if total or rows:
        logger.info("vectors: embedded %d, payload-updated %d", total, len(rows))
    return total


def _row(r: asyncpg.Record) -> dict:
    d = dict(r)
    if isinstance(d.get("library_refs"), str):
        d["library_refs"] = json.loads(d["library_refs"])
    return d


async def drop_orphans(pool: asyncpg.Pool, qdrant: QdrantClient, keys: list[str]) -> None:
    if not keys:
        return
    qdrant.delete(ITEMS_COLLECTION, points_selector=qm.PointIdsList(points=[canon_point_id(k) for k in keys]))
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM canon_item WHERE canon_key = ANY($1::text[])", keys)
    logger.info("removed %d superseded canonical items", len(keys))


async def mark_all_for_reembed(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE canon_item SET embedded_hash = NULL, synced_payload_hash = NULL")
