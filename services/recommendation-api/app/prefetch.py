"""Predictive prefetch: what is this person likely to start within the next N hours?

Feeds Vesper's storage promotion (cold -> hot) and mobile smart downloads. Three kinds of
items, each with a likelihood:

  in_progress  — started in the last 30 days, not finished
  series_next  — a show they've been watching episodes of (the client resolves the next
                 episode: it knows the order, Muse doesn't need to)
  recommended  — top picks, at a lower base rate

…multiplied by how likely they are to watch anything at all in the window, from their own
weekday x hour pattern over the last 90 days.
"""

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg

ACTIVITY_SQL = """
SELECT "timestamp" FROM event
WHERE user_id = $1 AND action IN ('played','completed') AND item_type IN ('movie','series_episode')
  AND "timestamp" > now() - interval '90 days'
"""

PROGRESS_SQL = """
SELECT e.source, e.source_item_id, e.item_type, max(e.progress_pct) AS progress, max(e."timestamp") AS ts,
       (array_agg(e.metadata->>'series_id') FILTER (WHERE e.metadata ? 'series_id'))[1] AS series_id,
       (array_agg(e.metadata->>'series_name') FILTER (WHERE e.metadata ? 'series_name'))[1] AS series_name,
       (array_agg(e.metadata->>'title'))[1] AS title,
       bool_or(e.action = 'completed') AS completed
FROM event e
WHERE e.user_id = $1 AND e.action IN ('played','completed') AND e.item_type IN ('movie','series_episode')
  AND e."timestamp" > now() - interval '30 days'
GROUP BY 1, 2, 3
"""


def slot_rates(timestamps: list[datetime], tz: ZoneInfo, now: datetime, days: int = 90) -> dict[tuple[int, int], float]:
    """P(watching in a given weekday/hour slot) = share of those weekdays in the window that
    had any viewing in that hour."""
    seen: set[tuple[datetime.date, int]] = set()
    if timestamps:  # a newer profile is judged on the weeks it has existed, not all 90 days
        days = min(days, max(7, (now - min(timestamps)).days + 1))
    for ts in timestamps:
        local = ts.astimezone(tz)
        seen.add((local.date(), local.hour))
    weeks = max(1.0, days / 7.0)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for d, h in seen:
        counts[(d.weekday(), h)] += 1
    return {k: min(1.0, v / weeks) for k, v in counts.items()}


def active_probability(rates: dict[tuple[int, int], float], start: datetime, hours: int, tz: ZoneInfo) -> float:
    """P(at least one viewing slot in the next `hours`), slots treated as independent."""
    p_none = 1.0
    t = start.astimezone(tz)
    for i in range(max(1, hours)):
        slot = t + timedelta(hours=i)
        p_none *= 1.0 - rates.get((slot.weekday(), slot.hour), 0.0)
    return 1.0 - p_none


def rank_items(rows: list[dict], now: datetime) -> list[dict]:
    """In-progress films/episodes and recently-followed series, each with a base likelihood."""
    out: list[dict] = []
    series: dict[str, dict] = {}
    for r in rows:
        age_days = max((now - r["ts"]).total_seconds() / 86400.0, 0.0)
        fresh = 0.5 ** (age_days / 7.0)
        if r["item_type"] == "series_episode" and r.get("series_id"):
            s = series.setdefault(r["series_id"], {"episodes": 0, "ts": r["ts"], "name": r.get("series_name") or "", "source": r["source"], "partial": None})
            s["episodes"] += 1
            s["ts"] = max(s["ts"], r["ts"])
            if not r["completed"] and 3 < (r["progress"] or 0) < 92:
                s["partial"] = r
            continue
        if not r["completed"] and 3 < (r["progress"] or 0) < 92:
            out.append({"source": r["source"], "source_item_id": r["source_item_id"], "item_type": r["item_type"],
                        "title": r.get("title") or "", "reason": "in_progress", "base": 0.9 * fresh, "progress": r["progress"]})
    for sid, s in series.items():
        age_days = max((now - s["ts"]).total_seconds() / 86400.0, 0.0)
        binge = min(1.0, s["episodes"] / 4.0)
        base = (0.55 + 0.4 * binge) * 0.5 ** (age_days / 5.0)
        item = {"source": s["source"], "source_item_id": sid, "item_type": "series", "title": s["name"], "reason": "series_next", "base": base}
        if s["partial"]:
            item["resume_item_id"] = s["partial"]["source_item_id"]
        out.append(item)
    return sorted(out, key=lambda x: -x["base"])


async def load(pool: asyncpg.Pool, user_id: str):
    async with pool.acquire() as conn:
        acts = await conn.fetch(ACTIVITY_SQL, uuid.UUID(user_id))
        rows = await conn.fetch(PROGRESS_SQL, uuid.UUID(user_id))
    return [a["timestamp"] for a in acts], [dict(r) for r in rows]


def tzinfo(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - unknown zone names fall back to UTC
        return ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
