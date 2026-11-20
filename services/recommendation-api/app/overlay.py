"""The user-control overlay: explore dial and mood. Sits on top of the automatic profile,
never mutates it, always reversible (docs/architecture.md)."""

import uuid
from datetime import datetime, timedelta, timezone

import asyncpg

from . import moods

DEFAULT_EXPLORE = 0.125


async def get(pool: asyncpg.Pool, user_id: str) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT explore_ratio, mood, mood_expires_at, updated_at FROM user_overlay WHERE user_id = $1", uuid.UUID(user_id)
        )
    if row is None:
        return {"explore_ratio": DEFAULT_EXPLORE, "mood": None, "mood_expires_at": None}
    mood, exp = row["mood"], row["mood_expires_at"]
    if mood and exp and exp < datetime.now(timezone.utc):
        mood, exp = None, None
    return {"explore_ratio": round(float(row["explore_ratio"]), 3), "mood": mood, "mood_expires_at": exp.isoformat() if exp else None}


async def put(pool: asyncpg.Pool, user_id: str, explore_ratio: float | None, mood: str | None, mood_hours: float | None, clear_mood: bool) -> dict:
    if mood is not None and mood not in moods.MOODS:
        raise ValueError(f"mood must be one of {sorted(moods.MOODS)}")
    expires = datetime.now(timezone.utc) + timedelta(hours=mood_hours or moods.DEFAULT_MOOD_HOURS) if mood else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_overlay (user_id, explore_ratio, mood, mood_expires_at, updated_at)
            VALUES ($1, coalesce($2::real, $5::real), $3::text, $4::timestamptz, now())
            ON CONFLICT (user_id) DO UPDATE SET
              explore_ratio = coalesce($2::real, user_overlay.explore_ratio),
              mood = CASE WHEN $6::boolean THEN NULL WHEN $3::text IS NOT NULL THEN $3::text ELSE user_overlay.mood END,
              mood_expires_at = CASE WHEN $6::boolean THEN NULL WHEN $3::text IS NOT NULL THEN $4::timestamptz ELSE user_overlay.mood_expires_at END,
              updated_at = now()
            """,
            uuid.UUID(user_id), explore_ratio, mood, expires, DEFAULT_EXPLORE, clear_mood,
        )
    return await get(pool, user_id)
