"""Embedding worker: the background half of Muse's taste engine. One loop, every
MUSE_EMBEDDING_INTERVAL_SECONDS:

  1. catalog    new events -> item rows (canonical identity, episode -> series)
  2. enrich     TMDB keywords/credits/recommendations (if TMDB_API_KEY), LLM descriptors (if configured)
  3. canon      rebuild documents + tags for titles whose items changed
  4. vectors    embed changed documents (local model on CPU/GPU, or an OpenAI-compatible endpoint)
  5. profiles   multi-interest taste profiles for users with new events (+ everyone daily, for decay)
  6. ranker     daily: re-fit ranker weights from logged impressions

No web server; there's nothing to ask it. See docs/recommender.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
from muse_taste import embedder as embedder_mod
from muse_taste import llm as llm_mod
from muse_taste.tmdb import TMDBClient
from qdrant_client import QdrantClient

import catalog
import embed
import enrich
import profiles
import questions_producer
import ranker_fit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("muse.worker")

from muse_schema.config import database_url, secret
from muse_schema.crypto import get_vault
QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
RUN_INTERVAL_SECONDS = int(os.environ.get("MUSE_EMBEDDING_INTERVAL_SECONDS", "300"))
FULL_PROFILE_HOURS = int(os.environ.get("MUSE_FULL_PROFILE_HOURS", "24"))
RANKER_FIT_HOURS = int(os.environ.get("MUSE_RANKER_FIT_HOURS", "24"))
TMDB_CREDENTIAL = os.environ.get("TMDB_API_KEY") or os.environ.get("TMDB_READ_TOKEN")
TMDB_LANGUAGE = os.environ.get("MUSE_TMDB_LANGUAGE", "en-US")


async def json_codecs(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Worker:
    def __init__(self, pool: asyncpg.Pool, qdrant: QdrantClient, embedder, llm=None, tmdb: TMDBClient | None = None):
        self.pool = pool
        self.qdrant = qdrant
        self.embedder = embedder
        self.llm = llm
        self.tmdb = tmdb

    async def prepare(self) -> None:
        """Collection + model bookkeeping. A model or dimension change re-embeds everything."""
        recreated = embed.ensure_collection(self.qdrant, self.embedder.dim)
        async with self.pool.acquire() as conn:
            st = await conn.fetchrow("SELECT model_name, vector_dim FROM embedding_worker_state")
            if recreated or st["model_name"] != self.embedder.name or st["vector_dim"] != self.embedder.dim:
                if st["model_name"]:
                    logger.warning("embedding model changed (%s/%s -> %s/%s): re-embedding the catalog",
                                   st["model_name"], st["vector_dim"], self.embedder.name, self.embedder.dim)
                await conn.execute("UPDATE item SET updated_at = now()")  # rebuild docs -> new doc_hash
                await conn.execute("UPDATE canon_item SET embedded_hash = NULL, synced_payload_hash = NULL")
                await conn.execute("UPDATE embedding_worker_state SET model_name = $1, vector_dim = $2", self.embedder.name, self.embedder.dim)

    async def run_once(self) -> dict:
        report: dict = {}
        async with self.pool.acquire() as conn:
            st = await conn.fetchrow("SELECT last_processed_at, last_full_profile_at, last_ranker_fit_at FROM embedding_worker_state")
            touched, max_ts = await catalog.sync_items(conn, st["last_processed_at"])
        report["touched_users"] = len(touched)
        try:
            report["questions_expired"] = await questions_producer.expire(self.pool)
        except Exception:  # optional feature: a missing migration 009 must not stop the worker
            logger.exception("question expiry failed (ignored)")

        if self.tmdb is not None:
            report["tmdb"] = await enrich.enrich_tmdb(self.pool, self.tmdb)
        if self.llm is not None:
            report["llm_items"] = await enrich.describe_items(self.pool, self.llm)

        async with self.pool.acquire() as conn:
            report["canon_rebuilt"] = await catalog.rebuild_canon(conn, self.embedder.name)
            orphans = await catalog.orphaned_canon_keys(conn)
        await embed.drop_orphans(self.pool, self.qdrant, orphans)
        report["embedded"] = await embed.sync_vectors(self.pool, self.qdrant, self.embedder)

        now = datetime.now(timezone.utc)
        full = st["last_full_profile_at"] is None or now - st["last_full_profile_at"] > timedelta(hours=FULL_PROFILE_HOURS)
        users = set(touched)
        if full:
            async with self.pool.acquire() as conn:
                users |= {str(r["user_id"]) for r in await conn.fetch("SELECT DISTINCT user_id FROM event WHERE action <> 'cataloged'")}
        report["profiles"] = await profiles.recompute_users(self.pool, self.qdrant, users, self.embedder, self.llm, self.embedder.name)

        async with self.pool.acquire() as conn:
            if max_ts > st["last_processed_at"]:
                await conn.execute("UPDATE embedding_worker_state SET last_processed_at = $1", max_ts)
            if full:
                await conn.execute("UPDATE embedding_worker_state SET last_full_profile_at = now()")
        if st["last_ranker_fit_at"] is None or now - st["last_ranker_fit_at"] > timedelta(hours=RANKER_FIT_HOURS):
            report["ranker"] = await ranker_fit.fit(self.pool)
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE embedding_worker_state SET last_ranker_fit_at = now()")
        return report


async def heartbeat(pool: asyncpg.Pool, report: dict, ok: bool) -> None:
    """For `muse status` and the status page (counts only, never names)."""
    try:
        async with pool.acquire() as conn:
            if ok:
                await conn.execute("UPDATE embedding_worker_state SET last_cycle_at = now(), last_cycle_report = $1::jsonb",
                                   {k: v for k, v in report.items() if isinstance(v, (int, float, str, bool)) or v is None})
            else:
                await conn.execute("UPDATE embedding_worker_state SET last_error_at = now()")
    except Exception:  # pre-010 database
        pass


async def main() -> None:
    embedder = embedder_mod.from_env()
    llm = llm_mod.from_env()
    if llm is not None:
        logger.info("LLM enrichment on: %s (%s)", llm.name, "external" if llm.external else "local")
    get_vault()  # fail fast without the master key
    qdrant = QdrantClient(url=QDRANT_URL, api_key=secret("MUSE_QDRANT_API_KEY") or None)
    pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=5, init=json_codecs)
    http = httpx.AsyncClient(timeout=20.0)
    tmdb = TMDBClient(TMDB_CREDENTIAL, http, language=TMDB_LANGUAGE) if TMDB_CREDENTIAL else None
    if tmdb is None:
        logger.info("TMDB enrichment off (set TMDB_API_KEY): content understanding uses library metadata only")
    worker = Worker(pool, qdrant, embedder, llm, tmdb)
    await worker.prepare()
    logger.info("embedding worker started: model=%s dim=%d interval=%ds", embedder.name, embedder.dim, RUN_INTERVAL_SECONDS)
    while True:
        start = time.monotonic()
        try:
            report = await worker.run_once()
            report["seconds"] = round(time.monotonic() - start, 1)
            logger.info("cycle done in %.1fs: %s", report["seconds"], report)
            await heartbeat(pool, report, ok=True)
        except Exception:
            logger.exception("embedding worker cycle failed")
            await heartbeat(pool, {}, ok=False)
        await asyncio.sleep(max(RUN_INTERVAL_SECONDS - (time.monotonic() - start), 5))


if __name__ == "__main__":
    asyncio.run(main())
