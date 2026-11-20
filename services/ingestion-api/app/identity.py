"""Alias -> canonical user_id resolution for linked identities (see migration 007).

Small in-process cache: links change rarely (a person pairs Vesper and Mono once), and a
stale entry for up to TTL seconds only means a few events land on the alias and get moved
over by the next link/merge.
"""

import time
import uuid

import asyncpg

TTL_SECONDS = 30.0
_cache: dict[uuid.UUID, tuple[uuid.UUID, float]] = {}


async def canonical(conn: asyncpg.Connection, user_id: uuid.UUID) -> uuid.UUID:
    hit = _cache.get(user_id)
    now = time.monotonic()
    if hit and now - hit[1] < TTL_SECONDS:
        return hit[0]
    try:
        row = await conn.fetchval("SELECT user_id FROM identity_link WHERE alias_user_id = $1", user_id)
    except asyncpg.UndefinedTableError:
        row = None  # migration 007 not applied yet (the recommendation API applies it on start)
    target = row or user_id
    _cache[user_id] = (target, now)
    return target


def forget() -> None:
    _cache.clear()
