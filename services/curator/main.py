"""Curator — autonomous storage-budget manager. See docs/architecture.md
(Discovery/acquisition pipeline) for the full design this implements.

Two cycles on independent intervals:

- Frequent (default every 30 min): re-checks everything on Soon Gone against the protection
  rules (watched, on someone's list, asked for, favourite, pinned: budget.protection) and
  saves what now qualifies; removes what is past its grace window and still unprotected.
- Slow (default daily, or right away when the admin asks): syncs real sizes from
  Radarr/Sonarr, adopts titles a person tagged "muse", monitors the next season of series the
  household is watching, adds what the household would like within the budget, the daily cap
  and the free disk space, and, only when over budget, puts the least wanted on Soon Gone.

Everything it does lands in curator_activity (the dashboard) and in the admin's feed
(Vesper; muse_schema.notify.admin), with "keep" / "remove now" actions.

Muse never touches files directly — every add/remove goes through arr_client.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import asyncpg
import httpx
from muse_schema import notify
from muse_schema.crypto import P_CURATOR, get_vault
from muse_taste import embedder as embedder_mod
from muse_taste.tmdb import TMDBClient
from qdrant_client import QdrantClient

from arr_client import ArrConfig, add_wanted, free_bytes, list_items, monitor_next_season, remove_item
from budget import (
    MIN_FREE_BYTES,
    PROTECTION_TEXT,
    SOON_GONE_GRACE_HOURS,
    finalize_removal,
    get_active_users,
    get_budget_bytes,
    get_current_quota_usage_bytes,
    get_downloads_today,
    get_dynamic_daily_cap,
    get_expired_soon_gone,
    get_pending_soon_gone,
    get_settings,
    mark_soon_gone,
    protection,
    record_download,
    save_from_soon_gone,
)
from discovery import ESTIMATED_BYTES, SEASON_ESTIMATED_BYTES, catalog_candidates, trending_candidates
from scoring import ADD_SCORE_THRESHOLD, REMOVE_SCORE_THRESHOLD, ProfileScorer, add_score, canon_for, remove_score

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.curator")

from muse_schema.config import database_url, secret

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")

SOON_GONE_CHECK_INTERVAL_SECONDS = int(os.environ.get("MUSE_SOON_GONE_CHECK_INTERVAL_SECONDS", "1800"))
ADD_REMOVE_CYCLE_INTERVAL_SECONDS = int(os.environ.get("MUSE_CURATOR_CYCLE_INTERVAL_SECONDS", "86400"))
NEXT_SEASON_WINDOW_DAYS = int(os.environ.get("MUSE_CURATOR_NEXT_SEASON_DAYS", "60"))
NEXT_SEASON_SLACK = int(os.environ.get("MUSE_CURATOR_NEXT_SEASON_SLACK", "2"))  # episodes left when the next season is fetched

ITEM_TYPES = ["movie", "series_episode"]  # arr-side types: Radarr movies, Sonarr series

TMDB_API_KEY = secret("TMDB_API_KEY") or secret("TMDB_READ_TOKEN")
TRAKT_CLIENT_ID = secret("TRAKT_CLIENT_ID")

KIND_WORD = {"movie": "film", "series_episode": "series"}


def _label(data: dict | None, item_type: str, tmdb_id: str) -> str:
    data = data or {}
    if data.get("title"):
        return f"{data['title']} ({data['year']})" if data.get("year") else str(data["title"])
    return f"{KIND_WORD.get(item_type, item_type)} tmdb:{tmdb_id}"


async def log_activity(pool: asyncpg.Pool, kind: str, item_type: str | None, tmdb_id: str | None, data: dict | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO curator_activity (kind, item_type, source_item_id, data) VALUES ($1, $2, $3, $4)",
            kind, item_type, tmdb_id, get_vault().seal_json(data or {}, P_CURATOR),
        )


async def tell_admin(pool: asyncpg.Pool, kind: str, title: str, body: str, *, item_type=None, tmdb_id=None, severity="info",
                     actions=None, key=None, extra=None) -> None:
    try:
        async with pool.acquire() as conn:
            await notify.admin(conn, f"curator.{kind}", "video", title, body, severity=severity, item_type=item_type,
                               source_item_id=tmdb_id, actions=actions, key=key, extra=extra)
    except Exception:  # a feed hiccup must never stop the curator
        logger.exception("could not add an admin notification")


async def _managed_data(pool: asyncpg.Pool, item_type: str, tmdb_id: str) -> dict:
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT data FROM curator_managed_item WHERE source_item_id = $1 AND item_type = $2 ORDER BY added_at DESC LIMIT 1", tmdb_id, item_type,
        )
    return get_vault().open_json(raw, P_CURATOR) or {} if raw else {}


# --- frequent: Soon Gone ---------------------------------------------------------------------------

async def run_soon_gone_checks(pool: asyncpg.Pool, arr_client: httpx.AsyncClient | None, arr_config: ArrConfig | None, paused: bool = False) -> dict:
    """Save what is now protected; remove what is expired and still not."""
    saved, removed = [], []
    async with pool.acquire() as conn:
        forced = {str(r["id"]) for r in await conn.fetch("SELECT id FROM soon_gone WHERE forced AND whitelisted = FALSE AND removed = FALSE")}
    for row in await get_pending_soon_gone(pool):
        if str(row["id"]) in forced:
            continue
        reason = await protection(pool, row["item_type"], row["source_item_id"])
        if reason:
            await save_from_soon_gone(pool, row["id"], reason)
            data = await _managed_data(pool, row["item_type"], row["source_item_id"])
            label = _label(data, row["item_type"], row["source_item_id"])
            await log_activity(pool, "saved", row["item_type"], row["source_item_id"], {**data, "reason": reason})
            await tell_admin(pool, "saved", f"{label} stays", f"Taken off Soon Gone: {PROTECTION_TEXT.get(reason, reason)}.",
                             item_type=row["item_type"], tmdb_id=row["source_item_id"], key=f"curator:saved:{row['id']}")
            saved.append(row["source_item_id"])
    if paused:
        return {"saved": saved, "removed": removed}
    for row in await get_expired_soon_gone(pool):
        if not row["arr_id"]:
            logger.warning("soon-gone item has no arr_id on record, skipping removal")
            continue
        if arr_client is None or arr_config is None or not arr_config.enabled(row["item_type"]):
            continue
        data = await _managed_data(pool, row["item_type"], row["source_item_id"])
        logger.info("soon-gone grace expired, removing arr_id=%s", row["arr_id"])
        try:
            await remove_item(arr_client, arr_config, row["item_type"], row["arr_id"])
        except httpx.HTTPError:
            logger.exception("removal failed, will retry next check")
            continue
        await finalize_removal(pool, str(row["id"]), row["source_item_id"], row["item_type"])
        label = _label(data, row["item_type"], row["source_item_id"])
        await log_activity(pool, "removed", row["item_type"], row["source_item_id"], data)
        await tell_admin(pool, "removed", f"{label} removed", "Nobody watched it during Soon Gone, so the space went back to the budget.",
                         item_type=row["item_type"], tmdb_id=row["source_item_id"], key=f"curator:removed:{row['id']}")
        removed.append(row["source_item_id"])
    return {"saved": saved, "removed": removed}


# kept for callers of the earlier name
async def run_soon_gone_expiry(pool, arr_client, arr_config):
    return await run_soon_gone_checks(pool, arr_client, arr_config)


# --- slow: sync, adopt, seasons --------------------------------------------------------------------

async def run_sync(pool: asyncpg.Pool, arr_client: httpx.AsyncClient, arr_config: ArrConfig) -> dict:
    """Real sizes, titles removed by hand, and titles tagged by a person (adopted)."""
    out = {"synced": 0, "adopted": 0, "gone": 0}
    for item_type in ITEM_TYPES:
        if not arr_config.enabled(item_type):
            continue
        items = await list_items(arr_client, arr_config, item_type)
        by_arr = {i.arr_id: i for i in items}
        async with pool.acquire() as conn:
            managed = await conn.fetch(
                "SELECT id, source_item_id, arr_id FROM curator_managed_item WHERE item_type = $1 AND removed_at IS NULL", item_type,
            )
            known = {r["source_item_id"] for r in managed}
            for r in managed:
                it = by_arr.get(r["arr_id"] or "")
                if it is None:
                    await conn.execute("UPDATE curator_managed_item SET removed_at = now() WHERE id = $1", r["id"])
                    out["gone"] += 1
                    continue
                await conn.execute(
                    "UPDATE curator_managed_item SET size_bytes = $2, size_synced_at = now(), seasons_monitored = $3, seasons_total = $4 WHERE id = $1",
                    r["id"], it.size_bytes, it.seasons_monitored, it.seasons_total,
                )
                out["synced"] += 1
            for it in items:
                if not it.tagged or not it.tmdb_id or it.tmdb_id in known:
                    continue
                data = {"title": it.title, "year": it.year, "reason": "tagged in Radarr/Sonarr"}
                await conn.execute(
                    """INSERT INTO curator_managed_item (source, source_item_id, item_type, estimated_bytes, arr_id, size_bytes, size_synced_at,
                                                         seasons_monitored, seasons_total, adopted, data)
                       VALUES ('arr', $1, $2, $3, $4, $5, now(), $6, $7, TRUE, $8)
                       ON CONFLICT (source, source_item_id) DO UPDATE SET removed_at = NULL, arr_id = EXCLUDED.arr_id, adopted = TRUE,
                           size_bytes = EXCLUDED.size_bytes, data = EXCLUDED.data""",
                    it.tmdb_id, item_type, it.size_bytes or ESTIMATED_BYTES[item_type], it.arr_id, it.size_bytes,
                    it.seasons_monitored, it.seasons_total, get_vault().seal_json(data, P_CURATOR),
                )
                out["adopted"] += 1
                await log_activity(pool, "adopted", item_type, it.tmdb_id, data)
    return out


async def run_season_cycle(pool: asyncpg.Pool, arr_client: httpx.AsyncClient, arr_config: ArrConfig, headroom: int) -> list[str]:
    """Fetch the next season of a series once the household has nearly watched what is there."""
    if not arr_config.enabled("series_episode"):
        return []
    items = {i.arr_id: i for i in await list_items(arr_client, arr_config, "series_episode")}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_item_id, arr_id, data FROM curator_managed_item WHERE item_type = 'series_episode' AND removed_at IS NULL",
        )
    done = []
    for r in rows:
        it = items.get(r["arr_id"] or "")
        if it is None or not it.seasons_total or (it.seasons_monitored or 0) >= it.seasons_total or it.episode_files == 0:
            continue
        async with pool.acquire() as conn:
            watched = await conn.fetchval(
                """SELECT count(DISTINCT i.item_key) FROM event e JOIN item i ON i.item_key = e.source || ':' || e.source_item_id
                   LEFT JOIN item p ON p.item_key = i.parent_key
                   WHERE (i.parent_key = $1 OR p.canon_key = $1) AND (e.action = 'completed' OR (e.action = 'played' AND e.progress_pct >= 90))
                     AND e."timestamp" > now() - make_interval(days => $2::int)""",
                canon_for("series_episode", r["source_item_id"]), NEXT_SEASON_WINDOW_DAYS,
            )
        if watched < max(1, it.episode_files - NEXT_SEASON_SLACK):
            continue
        if headroom < SEASON_ESTIMATED_BYTES:
            logger.info("next season wanted but no budget headroom")
            continue
        try:
            season = await monitor_next_season(arr_client, arr_config, r["arr_id"])
        except httpx.HTTPError:
            logger.exception("could not monitor the next season")
            continue
        if season is None:
            continue
        headroom -= SEASON_ESTIMATED_BYTES
        data = get_vault().open_json(r["data"], P_CURATOR) or {} if r["data"] else {}
        label = _label(data, "series_episode", r["source_item_id"])
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE curator_managed_item SET seasons_monitored = $2, estimated_bytes = estimated_bytes + $3 WHERE source_item_id = $1 AND item_type = 'series_episode' AND removed_at IS NULL",
                r["source_item_id"], season, SEASON_ESTIMATED_BYTES,
            )
        await log_activity(pool, "season_added", "series_episode", r["source_item_id"], {**data, "season": season})
        await tell_admin(pool, "season_added", f"{label}: season {season}", "The household is nearly through what's there, so the next season is on its way.",
                         item_type="series_episode", tmdb_id=r["source_item_id"], key=f"curator:season:{r['source_item_id']}:{season}")
        done.append(r["source_item_id"])
    return done


# --- slow: add and remove --------------------------------------------------------------------------

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
    budget = await get_budget_bytes(pool)
    usage = await get_current_quota_usage_bytes(pool)
    headroom = budget - usage
    if headroom <= 0:
        logger.info("no storage headroom (%d/%d bytes used), skipping add cycle", usage, budget)
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
            if arr_config is not None and not arr_config.enabled(item_type):
                continue
            free = await free_bytes(arr_client, arr_config, item_type) if arr_client is not None and arr_config is not None else None
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
                if free is not None and free - estimated_bytes < MIN_FREE_BYTES:
                    logger.info("disk nearly full, not adding more %s", item_type)
                    await tell_admin(pool, "disk_low", "Disk nearly full",
                                     f"Muse stopped adding {KIND_WORD[item_type]}s: {free / 1024**3:.0f} GB free, it keeps at least {MIN_FREE_BYTES / 1024**3:.0f} GB.",
                                     severity="warning", key=f"curator:disk_low:{item_type}:{datetime.now(timezone.utc):%Y-%m-%d}")
                    break
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
                tmdb_id = real.rsplit(":", 1)[1]
                reasons = [str(t) for t in (per_item[c.key][driving_user].top_tags or [])][:3]
                data = {"title": c.payload.get("title"), "year": c.payload.get("year"), "score": round(float(score), 3), "reasons": reasons,
                        "people": len([s for s in per_item[c.key].values() if s.score >= ADD_SCORE_THRESHOLD])}
                # managed items are tracked by TMDB id (what canon keys and enrichment speak)
                await record_download(pool, driving_user, tmdb_id, item_type, estimated_bytes, arr_id, data=data,
                                      seasons_monitored=1 if item_type != "movie" else None)
                label = _label(data, item_type, tmdb_id)
                await log_activity(pool, "added", item_type, tmdb_id, data)
                body = "Added because the household would like it" + (f" ({', '.join(reasons)})." if reasons else ".")
                if item_type != "movie":
                    body += " Season 1 first; the next one follows when you're watching."
                await tell_admin(pool, "added", f"{label} added", body, item_type=item_type, tmdb_id=tmdb_id,
                                 actions=["keep", "remove_now"], key=f"curator:added:{item_type}:{tmdb_id}:{arr_id}")
                added.append({"canon_key": c.key, "score": score, "user": driving_user})
                headroom -= estimated_bytes
                if free is not None:
                    free -= estimated_bytes
                if headroom <= 0:
                    break
    return added


async def run_remove_cycle(pool: asyncpg.Pool, qdrant: QdrantClient, active_users: list[str]) -> list[str]:
    budget = await get_budget_bytes(pool)
    usage = await get_current_quota_usage_bytes(pool)
    if usage <= budget:
        return []  # only prune when actually over budget
    scorer = await ProfileScorer.load(pool, qdrant, active_users)
    async with pool.acquire() as conn:
        managed = await conn.fetch(
            "SELECT source, source_item_id, item_type, coalesce(nullif(size_bytes, 0), estimated_bytes) AS bytes, data "
            "FROM curator_managed_item WHERE removed_at IS NULL"
        )
        pending = {(r["item_type"], r["source_item_id"]) for r in await conn.fetch(
            "SELECT item_type, source_item_id FROM soon_gone WHERE whitelisted = FALSE AND removed = FALSE")}
    # What is already on Soon Gone counts as freed (optimistic accounting pending grace expiry).
    usage -= sum(r["bytes"] for r in managed if (r["item_type"], r["source_item_id"]) in pending)
    rows = [r for r in managed if (r["item_type"], r["source_item_id"]) not in pending and not await protection(pool, r["item_type"], r["source_item_id"])]
    keys = {canon_for(r["item_type"], r["source_item_id"]): r for r in rows}
    vecs = scorer.vectors(list(keys))
    from muse_taste.ranking import CandidateInput

    per_item = scorer.score([CandidateInput(key=k, vector=v) for k, v in vecs.items()]) if scorer.profiles else {}
    scored = []
    for k, r in keys.items():
        best = remove_score(per_item.get(k, {}))
        if best is not None and best < REMOVE_SCORE_THRESHOLD:  # no vector yet: not judged, not removed
            scored.append((best, r))
    scored.sort(key=lambda pair: pair[0])  # worst first
    marked = []
    for _, row in scored:
        if usage <= budget:
            break
        if not await mark_soon_gone(pool, row["source_item_id"], row["item_type"]):
            continue
        data = get_vault().open_json(row["data"], P_CURATOR) or {} if row["data"] else {}
        label = _label(data, row["item_type"], row["source_item_id"])
        await log_activity(pool, "soon_gone", row["item_type"], row["source_item_id"], data)
        await tell_admin(pool, "soon_gone", f"{label} leaves in {SOON_GONE_GRACE_HOURS} hours",
                         "Over budget and nobody is likely to watch it. Watching it, putting it on a list or keeping it saves it.",
                         item_type=row["item_type"], tmdb_id=row["source_item_id"], severity="action", actions=["keep", "remove_now"],
                         key=f"curator:soon_gone:{row['item_type']}:{row['source_item_id']}:{datetime.now(timezone.utc):%Y-%m-%d}")
        marked.append(row["source_item_id"])
        usage -= row["bytes"]
    return marked


# --- loop -------------------------------------------------------------------------------------------

async def _json_codecs(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def save_status(pool: asyncpg.Pool, status: dict, cycle: bool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE curator_setting SET last_status = $1" + (", last_cycle_at = now(), run_requested_at = NULL" if cycle else "") + " WHERE id",
            status,
        )


async def run_slow_cycle(pool, qdrant, embedder, arr_client, arr_config, tmdb) -> dict:
    status: dict = {"at": datetime.now(timezone.utc).isoformat(), "errors": []}
    settings = await get_settings(pool)
    try:
        status["sync"] = await run_sync(pool, arr_client, arr_config)
    except (httpx.HTTPError, RuntimeError) as exc:
        status["errors"].append(f"sync: {exc.__class__.__name__}")
        await tell_admin(pool, "arr_error", "Can't reach Radarr/Sonarr", "Muse couldn't read Radarr/Sonarr; nothing is added or removed until it can.",
                         severity="warning", key=f"curator:arr_error:{datetime.now(timezone.utc):%Y-%m-%d}")
        return status
    if not settings["paused"]:
        active_users = await get_active_users(pool)
        if active_users:
            headroom = await get_budget_bytes(pool) - await get_current_quota_usage_bytes(pool)
            status["seasons"] = await run_season_cycle(pool, arr_client, arr_config, headroom)
            status["added"] = len(await run_add_cycle(pool, qdrant, embedder, arr_client, arr_config, tmdb, active_users))
            status["soon_gone"] = len(await run_remove_cycle(pool, qdrant, active_users))
        else:
            logger.info("no active users yet, skipping add/remove cycle")
    status["paused"] = settings["paused"]
    status["budget_bytes"] = await get_budget_bytes(pool)
    status["usage_bytes"] = await get_current_quota_usage_bytes(pool)
    status["free_bytes"] = {t: await free_bytes(arr_client, arr_config, t) for t in ITEM_TYPES if arr_config.enabled(t)}
    return status


async def main() -> None:
    embedder = embedder_mod.from_env()  # same model + settings as the embedding worker
    qdrant = QdrantClient(url=QDRANT_URL, api_key=secret("MUSE_QDRANT_API_KEY") or None)  # read-only key
    pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=5, init=_json_codecs)
    arr_config = ArrConfig.from_env()
    last_slow = float("-inf")
    logger.info("curator started (radarr=%s, sonarr=%s)", arr_config.enabled("movie"), arr_config.enabled("series_episode"))

    async with httpx.AsyncClient(timeout=15.0) as arr_client, httpx.AsyncClient(timeout=20.0) as tmdb_http:
        tmdb = TMDBClient(TMDB_API_KEY, tmdb_http) if TMDB_API_KEY else None
        while True:
            start = time.monotonic()
            try:
                settings = await get_settings(pool)
                await run_soon_gone_checks(pool, arr_client, arr_config, paused=settings["paused"])
                async with pool.acquire() as conn:
                    requested = await conn.fetchval("SELECT run_requested_at IS NOT NULL FROM curator_setting WHERE id")
                if requested or start - last_slow >= ADD_REMOVE_CYCLE_INTERVAL_SECONDS:
                    status = await run_slow_cycle(pool, qdrant, embedder, arr_client, arr_config, tmdb)
                    await save_status(pool, status, cycle=True)
                    last_slow = start
            except Exception:
                logger.exception("curator cycle failed")

            # wake early for "run now" from the admin
            deadline = start + SOON_GONE_CHECK_INTERVAL_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(30)
                try:
                    async with pool.acquire() as conn:
                        if await conn.fetchval("SELECT run_requested_at IS NOT NULL FROM curator_setting WHERE id"):
                            break
                except Exception:
                    break


if __name__ == "__main__":
    asyncio.run(main())
