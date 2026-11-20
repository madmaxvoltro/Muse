"""Identity resolution shared by every service that reads the event store.

One person can show up under several Muse user_ids: Jellyfin's user GUID, Mono's
per-account id, a SearxNG default id, ... Linking them (query API, link codes) writes a
`user_alias` row: alias_id -> canonical user_id. Events keep the id they were written with
(the store stays append-only, and unlinking is just deleting the alias row); readers
resolve at read time with these helpers, so taste vectors, "already seen" filtering and
profile summaries cover the whole person.

The functions take an asyncpg connection (or pool) but don't import asyncpg, so the shared
package keeps its single pydantic dependency.
"""

from __future__ import annotations

import uuid
from typing import Any


def as_uuid(user_id: str | uuid.UUID) -> uuid.UUID:
    return user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))


async def canonical_user(conn: Any, user_id: str | uuid.UUID) -> uuid.UUID:
    uid = as_uuid(user_id)
    row = await conn.fetchrow("SELECT user_id FROM user_alias WHERE alias_id = $1", uid)
    return row["user_id"] if row else uid


async def identity_group(conn: Any, user_id: str | uuid.UUID) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """(canonical id, every id that belongs to the same person, canonical first)."""
    canonical = await canonical_user(conn, user_id)
    rows = await conn.fetch("SELECT alias_id FROM user_alias WHERE user_id = $1 ORDER BY created_at", canonical)
    return canonical, [canonical, *(r["alias_id"] for r in rows if r["alias_id"] != canonical)]


async def canonical_map(conn: Any, user_ids: list[str | uuid.UUID]) -> dict[str, str]:
    """Bulk version for workers: str(id) -> str(canonical id)."""
    ids = [as_uuid(u) for u in user_ids]
    if not ids:
        return {}
    rows = await conn.fetch("SELECT alias_id, user_id FROM user_alias WHERE alias_id = ANY($1::uuid[])", ids)
    mapped = {str(r["alias_id"]): str(r["user_id"]) for r in rows}
    return {str(u): mapped.get(str(u), str(u)) for u in ids}
