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
import json
import logging
import os
import time

import asyncpg
import httpx
from muse_taste import embedder as embedder_mod
from muse_taste.tmdb import TMDBClient
from qdrant_client import QdrantClient

from arr_client import ArrConfig, add_wanted, remove_item
from budget import (
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
from discovery import ESTIMATED_BYTES, catalog_candidates, trending_candidates
from scoring import ADD_SCORE_THRESHOLD, REMOVE_SCORE_THRESHOLD, ProfileScorer, add_score, canon_for, remove_score

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.curator")

from muse_schema.config import database_url, secret
QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")

SOON_GONE_CHECK_INTERVAL_SECONDS = int(os.environ.get("MUSE_SOON_GONE_CHECK_INTERVAL_SECONDS", "1800"))
ADD_REMOVE_CYCLE_INTERVAL_SECONDS = int(os.environ.get("MUSE_CURATOR_CYCLE_INTERVAL_SECONDS", "86400"))

ITEM_TYPES = ["movie", "series_episode"]  # arr-side types: Radarr movies, Sonarr series

TMDB_API_KEY = os.environ.get("TMDB_API_KEY") or os.environ.get("TMDB_READ_TOKEN")
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID")


async def run_soon_gone_expiry(pool: asyncpg.Pool, arr_client: httpx.AsyncClient, arr_config: ArrConfig) -> None:
    expired = await get_expired_soon_gone(pool)
    for row in expired:
        if not row["arr_id"]:
            logger.warning("soon-gone item %s has no arr_id on record, skipping removal", row["source_item_id"])
            continue
        logger.info("soon-gone grace expired, removing %s (arr_id=%s)", row["source_item_id"], row["arr_id"])
        await remove_item(arr_client, arr_config, row["item_type"], row["arr_id"])
        await finalize_removal(pool, str(row["id"]), row["source_item_id"])


async def _arr_id_for(tmdb: TMDBClient | None, arr_item_type: str, real_canon: str | None) -> str | None:
    """Radarr adds by TMDB id; Sonarr by TVDB id, which needs a TMDB lookup."""
    if not real_canon or not real_canon.startswith("tmdb:"):
        return None
    tmdb_id = real_canon.rsplit(":", 1)[1]
    if arr_item_type == "movie":
        return tmdb_id
    if tmdb is None:
        return None
    details = await tmdb.details("tv", int(tmdb_id))
    return str(details["tvdb_id"]) if details and details.get("tvdb_id") else None


async def run_add_cycle(pool, qdrant, embedder, arr_client, arr_config, tmdb, active_users: list[str]) -> list[dict]:
    usage = await get_current_quota_usage_bytes(pool)
    headroom = STORAGE_BUDGET_BYTES - usage
    if headroom <= 0:
        logger.info("no storage headroom (%d/%d bytes used), skipping add cycle", usage, STORAGE_BUDGET_BYTES)
        return []
    scorer = await ProfileScorer.load(pool, qdrant, active_users)
    if not scorer.profiles:
        logger.info("no taste profiles yet, skipping add cycle")
        return []
    async with pool.acquire() as conn:
        managed = {canon_for(r["item_type"], r["source_item_id"]) for r in await conn.fetch("SELECT item_type, source_item_id FROM curator_managed_item")}
    skip = managed | scorer.seen_by_anyone()
    added: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as ext_client:
        for item_type in ITEM_TYPES:
            cands = catalog_candidates(qdrant, item_type)
            have = {c.key for c in cands}
            cands += [c for c in await trending_candidates(tmdb, ext_client, TRAKT_CLIENT_ID, embedder, item_type) if c.key not in have]
            cands = [c for c in cands if c.key not in skip]
            if not cands:
                continue
            per_item = scorer.score(cands)
            ranked = sorted(((add_score(per_item[c.key]), c) for c in cands), key=lambda t: -t[0])
            for score, c in ranked:
                if score < ADD_SCORE_THRESHOLD:
                    break  # sorted descending, nothing further qualifies
                estimated_bytes = ESTIMATED_BYTES.get(item_type, 2 * 1024**3)
                if estimated_bytes > headroom:
                    continue
                # The profile whose taste drove the score highest pays from its daily cap.
                driving_user = max(per_item[c.key].items(), key=lambda kv: kv[1].score)[0]
                cap = await get_dynamic_daily_cap(pool, driving_user)
                if await get_downloads_today(pool, driving_user) >= cap:
                    continue
                real = c.payload.get("real_key")
                arr_lookup_id = await _arr_id_for(tmdb, item_type, real)
                if arr_lookup_id is None:
                    logger.info("no arr lookup id for a candidate, skipping")
                    continue
                # logs are files too: an id, never a title or a person
                logger.info("adding %s %s (score=%.3f)", item_type, real, score)
                try:
                    arr_id = await add_wanted(ext_client, arr_config, item_type, arr_lookup_id)
                except (httpx.HTTPError, RuntimeError):
                    logger.exception("failed to add %s %s via arr, skipping", item_type, real)
                    continue
                # managed items are tracked by TMDB id (what canon keys and enrichment speak)
                await record_download(pool, driving_user, real.rsplit(":", 1)[1], item_type, estimated_bytes, arr_id)
                added.append({"canon_key": c.key, "score": score, "user": driving_user})
                headroom -= estimated_bytes
                if headroom <= 0:
                    break
    return added


async def run_remove_cycle(pool: asyncpg.Pool, qdrant: QdrantClient, active_users: list[str]) -> list[str]:
    usage = await get_current_quota_usage_bytes(pool)
    if usage <= STORAGE_BUDGET_BYTES:
        return []  # only prune when actually over budget
    scorer = await ProfileScorer.load(pool, qdrant, active_users)
    async with pool.acquire() as conn:
        managed = await conn.fetch("SELECT source, source_item_id, item_type FROM curator_managed_item WHERE removed_at IS NULL")
    rows = [r for r in managed if not await is_protected(pool, r["item_type"], r["source_item_id"])]
    keys = {canon_for(r["item_type"], r["source_item_id"]): r for r in rows}
    vecs = scorer.vectors(list(keys))
    from muse_taste.ranking import CandidateInput

    per_item = scorer.score([CandidateInput(key=k, vector=v) for k, v in vecs.items()]) if scorer.profiles else {}
    scored = []
    for k, r in keys.items():
        best = remove_score(per_item.get(k, {}))
        if best is not None and best < REMOVE_SCORE_THRESHOLD:
            scored.append((best, r))
    scored.sort(key=lambda pair: pair[0])  # worst first
    marked = []
    for _, row in scored:
        if usage <= STORAGE_BUDGET_BYTES:
            break
        await mark_soon_gone(pool, row["source_item_id"], row["item_type"])
        marked.append(row["source_item_id"])
        usage -= ESTIMATED_BYTES.get(row["item_type"], 2 * 1024**3)  # optimistic accounting pending grace expiry
    return marked


async def _json_codecs(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def main() -> None:
    embedder = embedder_mod.from_env()  # same model + settings as the embedding worker
    qdrant = QdrantClient(url=QDRANT_URL, api_key=secret("MUSE_QDRANT_API_KEY") or None)  # read-only key
    pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=5, init=_json_codecs)
    arr_config = ArrConfig.from_env()
    last_add_remove_cycle = float("-inf")
    logger.info("curator started")

    async with httpx.AsyncClient(timeout=15.0) as arr_client, httpx.AsyncClient(timeout=20.0) as tmdb_http:
        tmdb = TMDBClient(TMDB_API_KEY, tmdb_http) if TMDB_API_KEY else None
        while True:
            start = time.monotonic()
            try:
                await run_soon_gone_expiry(pool, arr_client, arr_config)

                if start - last_add_remove_cycle >= ADD_REMOVE_CYCLE_INTERVAL_SECONDS:
                    active_users = await get_active_users(pool)
                    if active_users:
                        await run_add_cycle(pool, qdrant, embedder, arr_client, arr_config, tmdb, active_users)
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
