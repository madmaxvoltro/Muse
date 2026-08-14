"""Core curator decision logic: dynamic per-user rate limits, the 300GB budget,
multi-profile ADD/REMOVE combination, and the "Soon Gone" grace mechanism.
See docs/architecture.md — Discovery/acquisition pipeline — this module is the
direct implementation of every decision documented there.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger("muse.curator.budget")

STORAGE_BUDGET_BYTES = 300 * 1024**3  # 300GB, the decided global ceiling
BASE_DAILY_CAP = 10  # decided: 10/day per user-profile, ceiling not a flat allowance
ACTIVITY_WINDOW_DAYS = 14  # rolling window used to scale the cap down for inactive users
SOON_GONE_GRACE_HOURS = 48
ADD_SCORE_THRESHOLD = 0.3  # minimum average taste-similarity to even be considered
PROTECT_PROGRESS_MIN = 10.0
PROTECT_PROGRESS_MAX = 90.0


async def get_active_users(pool: asyncpg.Pool, window_days: int = 30) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT user_id FROM event WHERE "timestamp" > now() - ($1 || ' days')::interval""",
            window_days,
        )
    return [str(r["user_id"]) for r in rows]


async def get_dynamic_daily_cap(pool: asyncpg.Pool, user_id: str) -> int:
    """Scales BASE_DAILY_CAP down based on recent activity — a rarely-active profile
    gets a much smaller slice of the shared quota than a heavily-active one.
    """
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT count(*) FROM event WHERE user_id = $1 AND "timestamp" > now() - ($2 || ' days')::interval""",
            uuid.UUID(user_id),
            ACTIVITY_WINDOW_DAYS,
        )
    # Simple linear scale: 50+ events in the window -> full cap; scales down from there.
    # Exact curve intentionally not over-engineered — see docs/architecture.md, this was
    # decided as "a mechanism", not a precisely specified formula.
    activity_ratio = min(count / 50.0, 1.0)
    return max(round(BASE_DAILY_CAP * activity_ratio), 0 if count == 0 else 1)


async def get_downloads_today(pool: asyncpg.Pool, user_id: str) -> int:
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM curator_download_log WHERE user_id = $1 AND downloaded_at > now() - interval '24 hours'",
            uuid.UUID(user_id),
        )
    return count


async def get_current_quota_usage_bytes(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT coalesce(sum(estimated_bytes), 0) FROM curator_managed_item WHERE removed_at IS NULL"
        )
    return int(total)


async def is_protected(pool: asyncpg.Pool, source: str, source_item_id: str) -> bool:
    """Favorited or partially-watched items are always excluded from auto-delete,
    regardless of score — decided together as the protection set.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM event
            WHERE source = $1 AND source_item_id = $2
              AND (
                action = 'favorited'
                OR (action = 'played' AND progress_pct BETWEEN $3 AND $4)
              )
            LIMIT 1
            """,
            source,
            source_item_id,
            PROTECT_PROGRESS_MIN,
            PROTECT_PROGRESS_MAX,
        )
    return row is not None


async def record_download(pool: asyncpg.Pool, user_id: str, source_item_id: str, item_type: str, estimated_bytes: int, arr_id: str) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO curator_download_log (user_id, source_item_id) VALUES ($1, $2)",
                uuid.UUID(user_id),
                source_item_id,
            )
            await conn.execute(
                """
                INSERT INTO curator_managed_item (source, source_item_id, item_type, estimated_bytes, arr_id)
                VALUES ('arr', $1, $2, $3, $4)
                ON CONFLICT (source, source_item_id) DO NOTHING
                """,
                source_item_id,
                item_type,
                estimated_bytes,
                arr_id,
            )


async def mark_soon_gone(pool: asyncpg.Pool, source_item_id: str, item_type: str) -> None:
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM soon_gone WHERE source_item_id = $1 AND whitelisted = FALSE AND removed = FALSE",
            source_item_id,
        )
        if existing:
            return  # already pending, don't restart the countdown
        arr_id = await conn.fetchval(
            "SELECT arr_id FROM curator_managed_item WHERE source_item_id = $1 AND removed_at IS NULL",
            source_item_id,
        )
        await conn.execute(
            "INSERT INTO soon_gone (source_item_id, item_type, arr_id, expires_at) VALUES ($1, $2, $3, now() + interval '48 hours')",
            source_item_id,
            item_type,
            arr_id,
        )
    logger.info("marked soon-gone: %s (%s), %dh grace", source_item_id, item_type, SOON_GONE_GRACE_HOURS)


async def get_expired_soon_gone(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source_item_id, item_type, arr_id FROM soon_gone WHERE expires_at < now() AND whitelisted = FALSE AND removed = FALSE"
        )
    return [dict(r) for r in rows]


async def finalize_removal(pool: asyncpg.Pool, soon_gone_id: str, source_item_id: str) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE soon_gone SET removed = TRUE WHERE id = $1", uuid.UUID(soon_gone_id))
            await conn.execute(
                "UPDATE curator_managed_item SET removed_at = now() WHERE source_item_id = $1 AND removed_at IS NULL",
                source_item_id,
            )
