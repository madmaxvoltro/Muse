"""Soon Gone endpoints backing: list what's pending, and whitelist-by-watching.

Whitelisting reuses the existing protection mechanism rather than inventing a new one —
it inserts a `favorited` event (see curator/budget.py is_protected(), which already
treats favorited items as always-excluded from auto-delete). This is called from a
trusted internal service (this API), not an adapter, so it writes directly rather than
going through the rate-limited ingestion API — see docs/architecture.md Security model
for why that distinction is safe (adapters are the untrusted write path, this isn't one).
"""

import json
import uuid
from datetime import datetime, timezone

import asyncpg


async def list_pending(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_item_id, item_type, marked_at, expires_at FROM soon_gone "
            "WHERE whitelisted = FALSE AND removed = FALSE ORDER BY expires_at ASC"
        )
    return [dict(r) for r in rows]


async def whitelist(pool: asyncpg.Pool, source_item_id: str, user_id: str) -> bool:
    """Called by the Jellyfin plugin's playback-start hook. Returns False if the item
    wasn't actually pending (nothing to whitelist — e.g. already expired or never marked).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, item_type FROM soon_gone WHERE source_item_id = $1 AND whitelisted = FALSE AND removed = FALSE",
                source_item_id,
            )
            if row is None:
                return False

            await conn.execute(
                "UPDATE soon_gone SET whitelisted = TRUE, whitelisted_by = $1 WHERE id = $2",
                uuid.UUID(user_id),
                row["id"],
            )
            await conn.execute(
                """
                INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, "timestamp", metadata)
                VALUES ($1, 'jellyfin', $2, $3, 'favorited', 1.5, $4, $5::jsonb)
                """,
                uuid.UUID(user_id),
                source_item_id,
                row["item_type"],
                datetime.now(timezone.utc),
                json.dumps({"via": "soon_gone_whitelist"}),
            )
    return True
