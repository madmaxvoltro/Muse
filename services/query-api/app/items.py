"""Item lookups shared by search, profile and questions."""

from __future__ import annotations

import uuid
from typing import Any

DOMAIN_LABELS = {
    "track": "music",
    "movie": "films",
    "series": "shows",
    "series_episode": "shows",
    "youtube_video": "YouTube videos",
    "audiobook": "audiobooks",
    "ebook": "books",
    "general": "anything",
}
ITEM_TYPES = ["movie", "series", "series_episode", "youtube_video", "track", "audiobook", "ebook"]
DOMAINS = ITEM_TYPES + ["general"]

# Actions that say something about taste (not 'cataloged', 'paused').
SIGNAL_ACTIONS = [
    "played", "completed", "skipped", "rejected", "favorited", "unfavorited", "searched", "added_watchlist",
    "removed", "detail_viewed", "trailer_played", "requested", "rated", "added_to_playlist",
]


def summarize_metadata(item_type: str, md: dict[str, Any]) -> dict[str, Any]:
    """The bits of an item's metadata worth showing a person or an assistant."""
    keep = ("title", "artist", "album", "year", "genres", "duration_s", "series", "season", "episode", "channel", "author", "language")
    out = {k: md[k] for k in keep if md.get(k) not in (None, "", [])}
    desc = md.get("description")
    if desc:
        out["description"] = desc if len(desc) <= 280 else desc[:277] + "..."
    return out


async def item_details(conn, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Latest non-empty metadata per (source, source_item_id)."""
    if not keys:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (e.source, e.source_item_id) e.source, e.source_item_id, e.item_type, e.metadata
        FROM event e
        JOIN unnest($1::text[], $2::text[]) AS k(source, sid) ON e.source = k.source AND e.source_item_id = k.sid
        WHERE e.metadata <> '{}'::jsonb
        ORDER BY e.source, e.source_item_id, e."timestamp" DESC
        """,
        [k[0] for k in keys],
        [k[1] for k in keys],
    )
    return {(r["source"], r["source_item_id"]): {"item_type": r["item_type"], "metadata": r["metadata"] or {}} for r in rows}


async def interactions(conn, group: list[uuid.UUID], keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """What this person did with each item: counts per action, last time, net weight."""
    if not keys:
        return {}
    rows = await conn.fetch(
        """
        SELECT e.source, e.source_item_id, e.action, count(*) AS n, max(e."timestamp") AS last, sum(e.action_weight) AS w
        FROM event e
        JOIN unnest($2::text[], $3::text[]) AS k(source, sid) ON e.source = k.source AND e.source_item_id = k.sid
        WHERE e.user_id = ANY($1::uuid[]) AND e.action <> 'cataloged'
        GROUP BY 1, 2, 3
        """,
        group,
        [k[0] for k in keys],
        [k[1] for k in keys],
    )
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        d = out.setdefault((r["source"], r["source_item_id"]), {"actions": {}, "last": None, "net_weight": 0.0})
        d["actions"][r["action"]] = r["n"]
        d["net_weight"] = round(d["net_weight"] + float(r["w"] or 0), 3)
        if d["last"] is None or r["last"] > d["last"]:
            d["last"] = r["last"]
    for d in out.values():
        d["last"] = d["last"].isoformat() if d["last"] else None
    return out
