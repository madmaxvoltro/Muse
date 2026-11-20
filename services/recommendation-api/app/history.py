"""A person's history, aggregated per item: what they finished, loved, rejected, were shown.
One SQL round trip per user per request; the aggregation rules are pure (aggregate()).
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import asyncpg

LOOKBACK_DAYS = 730

HISTORY_SQL = """
SELECT source, source_item_id, item_type,
  count(*) FILTER (WHERE action = 'completed')                         AS completions,
  bool_or(action = 'played')                                           AS played,
  bool_or(action = 'favorited')                                        AS favorited,
  bool_or(action = 'rejected')                                         AS rejected,
  bool_or(action = 'skipped')                                          AS skipped,
  bool_or(action = 'removed')                                          AS removed,
  bool_or(action = 'added_watchlist')                                  AS watchlisted,
  bool_or(action = 'requested')                                        AS requested,
  (array_agg(metadata->>'value' ORDER BY "timestamp" DESC) FILTER (WHERE action = 'rated'))[1] AS rating,
  max(progress_pct) FILTER (WHERE action = 'played')                   AS max_progress,
  max("timestamp") FILTER (WHERE action IN ('played','completed','rated','favorited','skipped','rejected')) AS last_ts,
  max("timestamp") FILTER (WHERE action = 'completed')                 AS last_completed,
  min("timestamp") FILTER (WHERE action IN ('played','completed'))     AS first_played,
  count(*) FILTER (WHERE action = 'impression' AND "timestamp" > now() - interval '14 days') AS impressions,
  count(*) FILTER (WHERE action IN ('detail_viewed','trailer_played','played','completed','added_watchlist')
                   AND "timestamp" > now() - interval '14 days')       AS recent_clicks,
  bool_or(coalesce((metadata->>'co_viewer')::boolean, false))           AS co_viewed,
  array_agg(metadata->'together') FILTER (WHERE jsonb_typeof(metadata->'together') = 'array') AS together
FROM event
WHERE user_id = $1 AND source_item_id IS NOT NULL AND action <> 'cataloged'
  AND "timestamp" > now() - make_interval(days => $2::int)
GROUP BY source, source_item_id, item_type
"""

SEARCHES_SQL = """
SELECT metadata->>'query' AS query, max("timestamp") AS ts FROM event
WHERE user_id = $1 AND action = 'searched' AND metadata ? 'query' AND "timestamp" > now() - interval '21 days'
GROUP BY 1 ORDER BY 2 DESC LIMIT 5
"""


@dataclass
class Item:
    source: str
    source_item_id: str
    item_type: str
    completions: int = 0
    played: bool = False
    favorited: bool = False
    rejected: bool = False
    skipped: bool = False
    removed: bool = False
    watchlisted: bool = False
    requested: bool = False
    rating: int = 0
    max_progress: float = 0.0
    last_ts: datetime | None = None
    last_completed: datetime | None = None
    first_played: datetime | None = None
    impressions: int = 0
    recent_clicks: int = 0
    co_viewed: bool = False
    together: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_item_id}"

    @property
    def disliked(self) -> bool:
        return self.rating < 0 or self.rejected

    @property
    def strength(self) -> float:
        """How much this item says about what the person likes (negative: dislikes)."""
        if self.disliked:
            return -1.0
        s = 0.0
        if self.rating == 2:
            s = 2.0
        elif self.rating == 1:
            s = 1.0
        if self.favorited:
            s = max(s, 1.5)
        if self.completions:
            s = max(s, 1.0) + 0.25 * min(self.completions - 1, 4)  # rewatches say a lot
        elif self.played and self.max_progress >= 25:
            s = max(s, 0.35)
        if self.skipped and not self.completions:
            s = min(s, -0.4)
        if self.co_viewed and self.rating == 0 and not self.favorited:
            s *= 0.6  # in the room, someone else chose
        return s

    @property
    def kind(self) -> str:
        """Which "Because you…" wording fits this item best."""
        if self.rating == 2:
            return "loved"
        if self.favorited:
            return "favorited"
        if self.rating == 1:
            return "liked"
        if self.completions:
            return "finished"
        return "watched"

    @property
    def seen(self) -> bool:
        """Consumed or judged: not something to recommend as new."""
        return bool(self.completions or self.played or self.rating or self.favorited or self.rejected or self.removed or self.skipped)


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def aggregate(rows: list[dict]) -> dict[str, Item]:
    out: dict[str, Item] = {}
    for r in rows:
        together: set[str] = set()
        for arr in r.get("together") or []:
            if isinstance(arr, list):
                together.update(str(x) for x in arr)
        it = Item(
            source=r["source"],
            source_item_id=r["source_item_id"],
            item_type=r["item_type"],
            completions=r.get("completions") or 0,
            played=bool(r.get("played")),
            favorited=bool(r.get("favorited")),
            rejected=bool(r.get("rejected")),
            skipped=bool(r.get("skipped")),
            removed=bool(r.get("removed")),
            watchlisted=bool(r.get("watchlisted")),
            requested=bool(r.get("requested")),
            rating=_int(r.get("rating")),
            max_progress=float(r.get("max_progress") or 0.0),
            last_ts=r.get("last_ts"),
            last_completed=r.get("last_completed"),
            first_played=r.get("first_played"),
            impressions=r.get("impressions") or 0,
            recent_clicks=r.get("recent_clicks") or 0,
            co_viewed=bool(r.get("co_viewed")),
            together=together,
        )
        out[it.key] = it
    return out


async def load(pool: asyncpg.Pool, user_id: str) -> tuple[dict[str, Item], list[str]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(HISTORY_SQL, uuid.UUID(user_id), LOOKBACK_DAYS)
        searches = await conn.fetch(SEARCHES_SQL, uuid.UUID(user_id))
    parsed = []
    for r in rows:
        d = dict(r)
        # asyncpg returns jsonb as text unless a codec is set; accept both.
        tg = d.get("together") or []
        d["together"] = [_json(x) for x in tg]
        parsed.append(d)
    return aggregate(parsed), [s["query"] for s in searches if s["query"]]


def _json(v):
    if isinstance(v, str):
        import json

        try:
            return json.loads(v)
        except ValueError:
            return []
    return v


def recency_weight(ts: datetime | None, half_life_days: float = 120.0) -> float:
    if ts is None:
        return 0.5
    age = max((datetime.now(timezone.utc) - ts).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age / half_life_days)
