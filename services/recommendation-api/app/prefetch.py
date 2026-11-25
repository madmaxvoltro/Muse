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

# Item ids are blind and metadata sealed at rest (muse_schema.storage): rows are opened in
# Python and aggregated here, one query per request.
EVENTS_SQL = """
SELECT source, source_item_id, item_type, action, progress_pct, "timestamp", metadata FROM event
WHERE user_id = ANY($1::uuid[]) AND action IN ('played','completed') AND item_type IN ('movie','series_episode')
  AND "timestamp" > now() - interval '90 days'
"""


def aggregate(events: list[dict], now: datetime, days: int = 30) -> list[dict]:
    """Opened events -> one row per item seen in the last `days`: max progress, last time,
    series id/name, title, completed."""
    cutoff = now - timedelta(days=days)
    per: dict[tuple, dict] = {}
    for e in events:
        if e["timestamp"] < cutoff:
            continue
        md = e.get("metadata") or {}
        k = (e["source"], e["source_item_id"], e["item_type"])
        r = per.setdefault(k, {"source": k[0], "source_item_id": k[1], "item_type": k[2], "progress": None, "ts": e["timestamp"],
                               "series_id": None, "series_name": None, "title": None, "completed": False})
        if e.get("progress_pct") is not None:
            r["progress"] = max(r["progress"] or 0, e["progress_pct"])
        r["ts"] = max(r["ts"], e["timestamp"])
        r["series_id"] = r["series_id"] or md.get("series_id")
        r["series_name"] = r["series_name"] or md.get("series_name") or md.get("series_title")
        r["title"] = r["title"] or md.get("title")
        r["completed"] = r["completed"] or e["action"] == "completed"
    return list(per.values())


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


async def load(pool: asyncpg.Pool, user_id: str) -> tuple[str, list[datetime], list[dict]]:
    """(canonical user id, viewing timestamps of 90 days, per-item rows of 30 days), over every
    identity linked to this person."""
    from muse_schema import identity_group
    from muse_schema.storage import fetch_open

    async with pool.acquire() as conn:
        canonical, group = await identity_group(conn, uuid.UUID(str(user_id)))
        events = await fetch_open(conn, EVENTS_SQL, list(group))
    return str(canonical), [e["timestamp"] for e in events], aggregate(events, now_utc())


def tzinfo(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 - unknown zone names fall back to UTC
        return ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
