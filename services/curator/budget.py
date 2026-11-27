"""Core curator decision logic: dynamic per-user rate limits, the storage budget,
multi-profile ADD/REMOVE combination, the protection rules and the "Soon Gone" grace
mechanism. See docs/architecture.md — Discovery/acquisition pipeline.

Protection (never removed, whatever the score), checked when an item is marked Soon Gone
and again right before it is actually removed:

* pinned by the admin ("keep" in Vesper / the dashboard);
* someone watched it recently (MUSE_CURATOR_RECENT_DAYS, default 30), for a series any
  episode of it;
* someone is part-way through it (10-90 %, within MUSE_CURATOR_IN_PROGRESS_DAYS, default 180);
* it is on someone's list (watchlist in Vesper, Jellyfin, Trakt, Letterboxd, IMDb...) and they
  haven't watched, removed or rejected it since. Radarr/Sonarr's own "wanted" list does not
  count: that is where the curator itself puts things;
* someone asked for it (`requested`, MUSE_CURATOR_REQUEST_DAYS, default 90) and hasn't watched it;
* it is a favourite, or someone's latest thumbs for it is "love".
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from muse_schema.crypto import P_DOWNLOAD_ITEM, get_vault

logger = logging.getLogger("muse.curator.budget")

GB = 1024**3
DEFAULT_BUDGET_GB = int(os.environ.get("MUSE_CURATOR_BUDGET_GB", "300"))
STORAGE_BUDGET_BYTES = DEFAULT_BUDGET_GB * GB  # the default; curator_setting.budget_gb overrides it
MIN_FREE_BYTES = int(float(os.environ.get("MUSE_CURATOR_MIN_FREE_GB", "50")) * GB)  # never fill the disk past this
BASE_DAILY_CAP = int(os.environ.get("MUSE_CURATOR_DAILY_CAP", "10"))  # per person per day, a ceiling, not an allowance
ACTIVITY_WINDOW_DAYS = 14  # rolling window used to scale the cap down for inactive users
SOON_GONE_GRACE_HOURS = int(os.environ.get("MUSE_CURATOR_GRACE_HOURS", "48"))
PROTECT_PROGRESS_MIN = 10.0
PROTECT_PROGRESS_MAX = 90.0
RECENT_DAYS = int(os.environ.get("MUSE_CURATOR_RECENT_DAYS", "30"))
IN_PROGRESS_DAYS = int(os.environ.get("MUSE_CURATOR_IN_PROGRESS_DAYS", "180"))
REQUEST_DAYS = int(os.environ.get("MUSE_CURATOR_REQUEST_DAYS", "90"))

# Sources whose `added_watchlist` is a library state, not a person's wish.
NOT_A_PERSONAL_LIST = {"arr"}


async def get_active_users(pool: asyncpg.Pool, window_days: int = 30) -> list[str]:
    """Active people, as canonical ids: linked accounts (user_alias) count as one person, whose
    taste vector the embedding worker stores under the canonical id."""
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """SELECT DISTINCT COALESCE(a.user_id, e.user_id) AS user_id FROM event e
                   LEFT JOIN user_alias a ON a.alias_id = e.user_id
                   WHERE e."timestamp" > now() - make_interval(days => $1::int)""",
                window_days,
            )
        except asyncpg.UndefinedTableError:  # database from before migration 007
            rows = await conn.fetch(
                """SELECT DISTINCT user_id FROM event WHERE "timestamp" > now() - make_interval(days => $1::int)""",
                window_days,
            )
    return [str(r["user_id"]) for r in rows]


async def get_dynamic_daily_cap(pool: asyncpg.Pool, user_id: str) -> int:
    """Scales BASE_DAILY_CAP down based on recent activity — a rarely-active profile
    gets a much smaller slice of the shared quota than a heavily-active one.
    """
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT count(*) FROM event WHERE user_id = $1 AND "timestamp" > now() - make_interval(days => $2::int)""",
            uuid.UUID(user_id),
            ACTIVITY_WINDOW_DAYS,
        )
    # Simple linear scale: 50+ events in the window -> full cap; scales down from there.
    activity_ratio = min(count / 50.0, 1.0)
    return max(round(BASE_DAILY_CAP * activity_ratio), 0 if count == 0 else 1)


async def get_downloads_today(pool: asyncpg.Pool, user_id: str) -> int:
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM curator_download_log WHERE user_id = $1 AND downloaded_at > now() - interval '24 hours'",
            uuid.UUID(user_id),
        )
    return count


async def get_settings(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow("SELECT paused, budget_gb FROM curator_setting WHERE id")
        except asyncpg.UndefinedTableError:
            row = None
    return {"paused": bool(row and row["paused"]), "budget_gb": (row["budget_gb"] if row and row["budget_gb"] else DEFAULT_BUDGET_GB)}


async def get_budget_bytes(pool: asyncpg.Pool) -> int:
    return int((await get_settings(pool))["budget_gb"]) * GB


async def get_current_quota_usage_bytes(pool: asyncpg.Pool) -> int:
    """Real size where Radarr/Sonarr reported one, the estimate until then."""
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT coalesce(sum(GREATEST(coalesce(size_bytes, 0), CASE WHEN size_bytes IS NULL OR size_bytes = 0 THEN estimated_bytes ELSE 0 END)), 0) "
            "FROM curator_managed_item WHERE removed_at IS NULL"
        )
    return int(total)


def protection_reason(rows: list[dict], now: datetime | None = None) -> str | None:
    """Why an item must stay, from its events (any user, its episodes included), or None.

    rows: dicts with user_id, source, action, action_weight, progress_pct, timestamp.
    """
    now = now or datetime.now(timezone.utc)
    recent = now - timedelta(days=RECENT_DAYS)
    by_user: dict[object, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["timestamp"]):
        by_user.setdefault(r["user_id"], []).append(r)

    for evs in by_user.values():
        for r in evs:
            if r["action"] in ("played", "completed") and r["timestamp"] >= recent:
                return "watched_recently"
    for evs in by_user.values():
        played = [r for r in evs if r["action"] == "played" and r["progress_pct"] is not None]
        if played:
            last = played[-1]
            if PROTECT_PROGRESS_MIN <= float(last["progress_pct"]) <= PROTECT_PROGRESS_MAX and last["timestamp"] >= now - timedelta(days=IN_PROGRESS_DAYS):
                if not any(r["action"] == "completed" and r["timestamp"] >= last["timestamp"] for r in evs):
                    return "in_progress"
    for evs in by_user.values():
        listed = [r for r in evs if r["action"] == "added_watchlist" and r["source"] not in NOT_A_PERSONAL_LIST]
        if listed:
            since = listed[-1]["timestamp"]
            if not any(r["action"] in ("completed", "removed", "rejected") and r["timestamp"] > since and r["source"] not in NOT_A_PERSONAL_LIST for r in evs):
                return "on_a_list"
    for evs in by_user.values():
        asked = [r for r in evs if r["action"] == "requested" and r["timestamp"] >= now - timedelta(days=REQUEST_DAYS)]
        if asked and not any(r["action"] in ("played", "completed") and r["timestamp"] > asked[-1]["timestamp"] for r in evs):
            return "requested"
    for evs in by_user.values():
        fav = [r for r in evs if r["action"] in ("favorited", "unfavorited")]
        if fav and fav[-1]["action"] == "favorited":
            return "favourite"
        rated = [r for r in evs if r["action"] == "rated"]
        if rated and (rated[-1]["action_weight"] or 0) >= 2:
            return "loved"
    return None


PROTECTION_TEXT = {
    "pinned": "kept by the admin",
    "watched_recently": "someone watched it recently",
    "in_progress": "someone is part-way through it",
    "on_a_list": "it is on someone's list",
    "requested": "someone asked for it",
    "favourite": "it is someone's favourite",
    "loved": "someone loved it",
}


async def protection(pool: asyncpg.Pool, item_type: str, tmdb_id: str) -> str | None:
    """The reason an item may not be removed, or None. Matched by canonical title (and, for
    a series, its episodes), so a list entry in Vesper or a favourite in Jellyfin protects the
    copy Radarr/Sonarr manages (ids are blind indexes at rest)."""
    from scoring import canon_for

    canon = canon_for(item_type, tmdb_id)
    async with pool.acquire() as conn:
        pinned = await conn.fetchval(
            "SELECT pinned FROM curator_managed_item WHERE source_item_id = $1 AND item_type = $2 AND removed_at IS NULL", tmdb_id, item_type,
        )
        if pinned:
            return "pinned"
        rows = await conn.fetch(
            """
            SELECT e.user_id, e.source, e.action, e.action_weight, e.progress_pct, e."timestamp"
            FROM event e JOIN item i ON i.item_key = e.source || ':' || e.source_item_id
            LEFT JOIN item p ON p.item_key = i.parent_key
            WHERE (i.canon_key = $1 OR i.parent_key = $1 OR p.canon_key = $1)
              AND e.action IN ('favorited', 'unfavorited', 'played', 'completed', 'rated', 'added_watchlist', 'removed', 'rejected', 'requested')
            ORDER BY e."timestamp" DESC LIMIT 2000
            """,
            canon,
        )
    return protection_reason([dict(r) for r in rows])


async def is_protected(pool: asyncpg.Pool, item_type: str, tmdb_id: str) -> bool:
    return await protection(pool, item_type, tmdb_id) is not None


async def record_download(pool: asyncpg.Pool, user_id: str, source_item_id: str, item_type: str, estimated_bytes: int, arr_id: str,
                          data: dict | None = None, seasons_monitored: int | None = None) -> None:
    from muse_schema.crypto import P_CURATOR

    async with pool.acquire() as conn:
        async with conn.transaction():
            # who caused which download is personal: the item is stored as a blind index
            await conn.execute(
                "INSERT INTO curator_download_log (user_id, source_item_id) VALUES ($1, $2)",
                uuid.UUID(user_id),
                get_vault().bid(f"{item_type}:{source_item_id}", P_DOWNLOAD_ITEM),
            )
            await conn.execute(
                """
                INSERT INTO curator_managed_item (source, source_item_id, item_type, estimated_bytes, arr_id, data, seasons_monitored)
                VALUES ('arr', $1, $2, $3, $4, $5, $6)
                ON CONFLICT (source, source_item_id) DO UPDATE SET removed_at = NULL, arr_id = EXCLUDED.arr_id,
                    estimated_bytes = EXCLUDED.estimated_bytes, data = EXCLUDED.data, size_bytes = NULL
                """,
                source_item_id,
                item_type,
                estimated_bytes,
                arr_id,
                get_vault().seal_json(data or {}, P_CURATOR),
                seasons_monitored,
            )


async def mark_soon_gone(pool: asyncpg.Pool, source_item_id: str, item_type: str) -> bool:
    """Start the grace window. False when it was already pending."""
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM soon_gone WHERE source_item_id = $1 AND item_type = $2 AND whitelisted = FALSE AND removed = FALSE",
            source_item_id, item_type,
        )
        if existing:
            return False  # already pending, don't restart the countdown
        arr_id = await conn.fetchval(
            "SELECT arr_id FROM curator_managed_item WHERE source_item_id = $1 AND item_type = $2 AND removed_at IS NULL",
            source_item_id, item_type,
        )
        await conn.execute(
            "INSERT INTO soon_gone (source_item_id, item_type, arr_id, expires_at) VALUES ($1, $2, $3, now() + make_interval(hours => $4::int))",
            source_item_id,
            item_type,
            arr_id,
            SOON_GONE_GRACE_HOURS,
        )
    logger.info("marked soon-gone: %s (%s), %dh grace", source_item_id, item_type, SOON_GONE_GRACE_HOURS)
    return True


async def save_from_soon_gone(pool: asyncpg.Pool, soon_gone_id, reason: str) -> None:
    """Cancel a pending removal because a protection rule now applies."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE soon_gone SET whitelisted = TRUE WHERE id = $1", soon_gone_id)
    logger.info("soon-gone %s saved: %s", soon_gone_id, reason)


async def get_expired_soon_gone(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source_item_id, item_type, arr_id FROM soon_gone WHERE expires_at < now() AND whitelisted = FALSE AND removed = FALSE"
        )
    return [dict(r) for r in rows]


async def get_pending_soon_gone(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source_item_id, item_type, arr_id, expires_at FROM soon_gone WHERE whitelisted = FALSE AND removed = FALSE"
        )
    return [dict(r) for r in rows]


async def finalize_removal(pool: asyncpg.Pool, soon_gone_id: str, source_item_id: str, item_type: str | None = None) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE soon_gone SET removed = TRUE WHERE id = $1", uuid.UUID(str(soon_gone_id)))
            await conn.execute(
                "UPDATE curator_managed_item SET removed_at = now() WHERE source_item_id = $1 AND ($2::text IS NULL OR item_type = $2) AND removed_at IS NULL",
                source_item_id,
                item_type,
            )
