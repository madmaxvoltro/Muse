"""Soon Gone: list what's pending, and whitelist-by-watching (or a Keep button).

The curator marks items by their tmdb (films) / tvdb (series) id, the ids Radarr/Sonarr
speak. Clients like Vesper and the Jellyfin plugin know Jellyfin item ids. Events carry
metadata.tmdb_id / tvdb_id (Jellyfin adapter), so each pending item is resolved to its
Jellyfin id here, and a whitelist call accepts either id.

Whitelisting inserts a `favorited` event under source 'arr' — the source the curator's
is_protected() checks for its managed items — so the item stays protected in later cycles
too, not only for this grace window.
"""

import json
import uuid
from datetime import datetime, timezone

import asyncpg

RESOLVE_SQL = """
SELECT source_item_id, metadata->>'title' AS title FROM event
WHERE source = 'jellyfin' AND (metadata->>'tmdb_id' = $1 OR metadata->>'tvdb_id' = $1)
  AND item_type = ANY($2::text[])
ORDER BY "timestamp" DESC LIMIT 1
"""

REVERSE_SQL = """
SELECT coalesce(metadata->>'tmdb_id', '') AS tmdb, coalesce(metadata->>'tvdb_id', '') AS tvdb FROM event
WHERE source = 'jellyfin' AND source_item_id = $1 AND (metadata ? 'tmdb_id' OR metadata ? 'tvdb_id')
ORDER BY "timestamp" DESC LIMIT 1
"""


def _types(item_type: str) -> list[str]:
    return ["series", "series_episode"] if item_type.startswith("series") else [item_type]


async def list_pending(pool: asyncpg.Pool) -> list[dict]:
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_item_id, item_type, marked_at, expires_at FROM soon_gone "
            "WHERE whitelisted = FALSE AND removed = FALSE ORDER BY expires_at ASC"
        )
        out = []
        for r in rows:
            d = dict(r)
            hit = await conn.fetchrow(RESOLVE_SQL, r["source_item_id"], _types(r["item_type"]))
            d["jellyfin_id"] = hit["source_item_id"] if hit else None
            d["title"] = hit["title"] if hit else None
            d["hours_left"] = max(0, round((r["expires_at"] - now).total_seconds() / 3600, 1))
            out.append(d)
    return out


async def whitelist(pool: asyncpg.Pool, item_id: str, user_id: str) -> bool:
    """Accepts the curator's tmdb/tvdb id or a Jellyfin item id. False if nothing was pending."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, source_item_id, item_type FROM soon_gone WHERE source_item_id = $1 AND whitelisted = FALSE AND removed = FALSE",
                item_id,
            )
            if row is None:
                ids = await conn.fetchrow(REVERSE_SQL, item_id)
                if ids:
                    row = await conn.fetchrow(
                        "SELECT id, source_item_id, item_type FROM soon_gone WHERE source_item_id = ANY($1::text[]) AND whitelisted = FALSE AND removed = FALSE",
                        [x for x in (ids["tmdb"], ids["tvdb"]) if x],
                    )
            if row is None:
                return False

            await conn.execute(
                "UPDATE soon_gone SET whitelisted = TRUE, whitelisted_by = $1 WHERE id = $2", uuid.UUID(user_id), row["id"]
            )
            await conn.execute(
                """
                INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, "timestamp", metadata)
                VALUES ($1, 'arr', $2, $3, 'favorited', 1.5, $4, $5::jsonb)
                """,
                uuid.UUID(user_id),
                row["source_item_id"],
                row["item_type"],
                datetime.now(timezone.utc),
                json.dumps({"via": "soon_gone_whitelist", "requested_as": item_id}),
            )
    return True
