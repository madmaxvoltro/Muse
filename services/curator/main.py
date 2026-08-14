"""Curator — autonomous 300GB storage-budget manager. See docs/architecture.md
(Discovery/acquisition pipeline) for the full design this implements.

Two cycles on independent intervals:
- Soon-Gone expiry check (frequent, default 30 min): finalizes removals whose 48h grace
  window has passed untouched. Whitelisting itself happens elsewhere — the Recommendation
  API exposes the whitelist endpoint the Jellyfin plugin's playback-start hook calls
  (see services/recommendation-api once that endpoint is added, and jellyfin-plugin/README.md).
- Add/remove cycle (slow, default daily): scores TMDB/Trakt candidates against active
  users' combined taste, adds within budget/rate-limits; scores currently-managed items
  for removal, moves the worst into the Soon Gone grace window.

Muse never touches files directly — every add/remove goes through arr_client.py, which
talks to Radarr/Sonarr's API.
"""

import asyncio
import logging
import os
import time

import asyncpg
import httpx
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

from arr_client import ArrConfig, add_wanted, remove_item
from budget import (
    ADD_SCORE_THRESHOLD,
    STORAGE_BUDGET_BYTES,
    finalize_removal,
    get_active_users,
    get_current_quota_usage_bytes,
    get_dynamic_daily_cap,
    get_downloads_today,
    get_expired_soon_gone,
    is_protected,
    mark_soon_gone,
    record_download,
)
from discovery import ESTIMATED_BYTES, fetch_candidates
from scoring import REMOVE_SCORE_THRESHOLD, embed_text, score_candidate_avg, score_owned_item_max

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.curator")

DATABASE_URL = os.environ["MUSE_DATABASE_URL"]
QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
MODEL_NAME = os.environ.get("MUSE_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

SOON_GONE_CHECK_INTERVAL_SECONDS = int(os.environ.get("MUSE_SOON_GONE_CHECK_INTERVAL_SECONDS", "1800"))
ADD_REMOVE_CYCLE_INTERVAL_SECONDS = int(os.environ.get("MUSE_CURATOR_CYCLE_INTERVAL_SECONDS", "86400"))

ITEM_TYPES = ["movie", "series_episode"]

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID")

ARR_CONFIG = ArrConfig(
    radarr_url=os.environ.get("RADARR_URL", ""),
    radarr_api_key=os.environ.get("RADARR_API_KEY", ""),
    sonarr_url=os.environ.get("SONARR_URL", ""),
    sonarr_api_key=os.environ.get("SONARR_API_KEY", ""),
)


async def run_soon_gone_expiry(pool: asyncpg.Pool, arr_client: httpx.AsyncClient) -> None:
    expired = await get_expired_soon_gone(pool)
    for row in expired:
        logger.info("soon-gone grace expired, removing %s", row["source_item_id"])
        await remove_item(arr_client, ARR_CONFIG, row["item_type"], row["source_item_id"])
        await finalize_removal(pool, str(row["id"]), row["source_item_id"])


async def run_add_cycle(
    pool: asyncpg.Pool, qdrant: QdrantClient, model: SentenceTransformer, arr_client: httpx.AsyncClient, active_users: list[str]
) -> None:
    usage = await get_current_quota_usage_bytes(pool)
    headroom = STORAGE_BUDGET_BYTES - usage
    if headroom <= 0:
        logger.info("no storage headroom (%d/%d bytes used), skipping add cycle", usage, STORAGE_BUDGET_BYTES)
        return

    async with httpx.AsyncClient(timeout=15.0) as ext_client:
        for item_type in ITEM_TYPES:
            candidates = await fetch_candidates(ext_client, TMDB_API_KEY, TRAKT_CLIENT_ID, item_type)
            scored = []
            for c in candidates:
                text = f"{c.title} {c.description}"
                vector = embed_text(model, text)
                score = score_candidate_avg(qdrant, vector, active_users, item_type)
                scored.append((score, c))
            scored.sort(key=lambda pair: pair[0], reverse=True)

            for score, c in scored:
                if score < ADD_SCORE_THRESHOLD:
                    break  # sorted descending, nothing further qualifies
                estimated_bytes = ESTIMATED_BYTES.get(item_type, 2 * 1024**3)
                if estimated_bytes > headroom:
                    continue

                # Attribute the download to whichever active profile's taste drove the
                # score highest, for the purpose of the per-user daily rate limit.
                driving_user = active_users[0] if active_users else None
                best = -1.0
                for uid in active_users:
                    uid_score = score_candidate_avg(qdrant, embed_text(model, f"{c.title} {c.description}"), [uid], item_type)
                    if uid_score > best:
                        best, driving_user = uid_score, uid
                if driving_user is None:
                    continue

                cap = await get_dynamic_daily_cap(pool, driving_user)
                already_today = await get_downloads_today(pool, driving_user)
                if already_today >= cap:
                    continue

                logger.info("adding %s '%s' (score=%.3f, driven by user=%s)", item_type, c.title, score, driving_user)
                await add_wanted(ext_client, ARR_CONFIG, item_type, c.source_item_id)
                await record_download(pool, driving_user, c.source_item_id, item_type, estimated_bytes)
                headroom -= estimated_bytes
                if headroom <= 0:
                    break


async def run_remove_cycle(pool: asyncpg.Pool, qdrant: QdrantClient, active_users: list[str]) -> None:
    usage = await get_current_quota_usage_bytes(pool)
    if usage <= STORAGE_BUDGET_BYTES:
        return  # only prune when actually over budget

    async with pool.acquire() as conn:
        managed = await conn.fetch(
            "SELECT source, source_item_id, item_type FROM curator_managed_item WHERE removed_at IS NULL"
        )

    scored = []
    for row in managed:
        if await is_protected(pool, row["source"], row["source_item_id"]):
            continue
        max_score = score_owned_item_max(qdrant, row["source"], row["source_item_id"], active_users, row["item_type"])
        if max_score is None:
            continue
        if max_score < REMOVE_SCORE_THRESHOLD:
            scored.append((max_score, row))

    scored.sort(key=lambda pair: pair[0])  # worst first
    for max_score, row in scored:
        if usage <= STORAGE_BUDGET_BYTES:
            break
        await mark_soon_gone(pool, row["source_item_id"], row["item_type"])
        usage -= ESTIMATED_BYTES.get(row["item_type"], 2 * 1024**3)  # optimistic accounting pending grace expiry


async def main() -> None:
    logger.info("loading embedding model %s", MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)
    qdrant = QdrantClient(url=QDRANT_URL)
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)

    last_add_remove_cycle = float("-inf")
    logger.info("curator started")

    async with httpx.AsyncClient(timeout=15.0) as arr_client:
        while True:
            start = time.monotonic()
            try:
                await run_soon_gone_expiry(pool, arr_client)

                if start - last_add_remove_cycle >= ADD_REMOVE_CYCLE_INTERVAL_SECONDS:
                    active_users = await get_active_users(pool)
                    if active_users:
                        await run_add_cycle(pool, qdrant, model, arr_client, active_users)
                        await run_remove_cycle(pool, qdrant, active_users)
                    else:
                        logger.info("no active users yet, skipping add/remove cycle")
                    last_add_remove_cycle = start
            except Exception:
                logger.exception("curator cycle failed")

            elapsed = time.monotonic() - start
            await asyncio.sleep(max(SOON_GONE_CHECK_INTERVAL_SECONDS - elapsed, 5))


if __name__ == "__main__":
    asyncio.run(main())
