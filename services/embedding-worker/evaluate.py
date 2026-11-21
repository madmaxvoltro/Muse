"""Offline evaluation on your own data: is Muse actually getting better?

    docker compose exec embedding-worker python evaluate.py [--k 10] [--holdout 0.2] [--domain video]

Replays every user's history with a temporal split (see muse_taste.evaluate): profiles are
built only from what happened before the cut, then scored on what they went on to like.
Prints Recall/nDCG/MRR/HitRate@K plus diversity, calibration and coverage for the legacy
single-vector approach and each stage of the current engine. Read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

import asyncpg
from muse_taste.evaluate import evaluate
from muse_taste.store import open_canon_data
from muse_schema.config import database_url, secret
from qdrant_client import QdrantClient

from profiles import EVENTS_SQL, fetch_vectors, to_signal_events
from worker import json_codecs


async def run(k: int, holdout: float, domain: str | None) -> None:
    pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=2, init=json_codecs)
    qdrant = QdrantClient(url=os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333"), api_key=secret("MUSE_QDRANT_API_KEY") or None)
    async with pool.acquire() as conn:
        q = "SELECT canon_key, item_type, domain, data FROM canon_item WHERE embedded_hash IS NOT NULL"
        raw = await conn.fetch(q + (" AND domain = $1" if domain else ""), *([domain] if domain else []))
        users = [str(r["user_id"]) for r in await conn.fetch("SELECT DISTINCT user_id FROM event WHERE action <> 'cataloged'")]
        events = {u: to_signal_events(await conn.fetch(EVENTS_SQL, uuid.UUID(u), 3650)) for u in users}
    rows = [{**open_canon_data(r["data"]), "canon_key": r["canon_key"], "item_type": r["item_type"], "domain": r["domain"]} for r in raw]
    catalog = {r["canon_key"]: {"item_type": r["item_type"], "domain": r["domain"], "vote_average": r.get("vote_average"), "vote_count": r.get("vote_count"),
                                "collection_id": r.get("collection_id"), "year": r.get("year")} for r in rows}
    print(f"loading {len(catalog)} vectors...")
    vectors = fetch_vectors(qdrant, list(catalog))
    tag_df: dict[str, int] = {}
    for r in rows:
        for t in r.get("tags") or []:
            tag_df[t] = tag_df.get(t, 0) + 1
    res = evaluate(
        events, catalog, vectors,
        tags={r["canon_key"]: list(r.get("tags") or []) for r in rows}, tag_df=tag_df,
        related_of={r["canon_key"]: list(r.get("related") or []) for r in rows},
        titles={r["canon_key"]: r.get("title") or "" for r in rows}, k=k, holdout_frac=holdout,
    )
    print(f"\nusers evaluated: {res.users} (need >= 5 liked titles each)\n")
    print(res.table())
    await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--holdout", type=float, default=0.2)
    ap.add_argument("--domain", default=None)
    a = ap.parse_args()
    asyncio.run(run(a.k, a.holdout, a.domain))
