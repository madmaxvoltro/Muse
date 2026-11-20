"""A person's year (Vesper Wrapped). Muse aggregates; the client adds artwork and palettes.

Minutes watched are estimated from runtime: a completion counts the full runtime, a partial
watch its furthest progress, once per item per day (sources overlap, see the taste collapse).
"""

import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg

YEAR_SQL = """
SELECT source, source_item_id, item_type, action, action_weight, progress_pct, "timestamp", metadata
FROM event
WHERE user_id = $1 AND item_type IN ('movie','series_episode','series')
  AND action IN ('played','completed','rated','favorited')
  AND "timestamp" >= $2 AND "timestamp" < $3
ORDER BY "timestamp"
"""

DEFAULT_RUNTIME = {"movie": 110, "series_episode": 42}


def _md(r: dict) -> dict:
    m = r.get("metadata") or {}
    if isinstance(m, str):
        import json

        m = json.loads(m)
    return m


def aggregate(rows: list[dict], tz: ZoneInfo, kids: bool = False) -> dict:
    per_day: dict[tuple[str, date], dict] = {}
    ratings: dict[str, int] = {}
    loved: set[str] = set()
    titles: dict[str, dict] = {}
    for r in rows:
        md = _md(r)
        key = f"{r['source']}:{r['source_item_id']}"
        titles.setdefault(key, {"source_item_id": r["source_item_id"], "item_type": r["item_type"], "title": md.get("title", ""),
                                "series_id": md.get("series_id"), "series_name": md.get("series_name"), "genres": md.get("genres") or [],
                                "runtime": md.get("runtime_minutes")})
        if md.get("runtime_minutes"):
            titles[key]["runtime"] = md["runtime_minutes"]
        if r["action"] == "rated":
            try:
                ratings[key] = int(md.get("value", 0))
            except (TypeError, ValueError):
                pass
            continue
        if r["action"] == "favorited":
            loved.add(key)
            continue
        local = r["timestamp"].astimezone(tz)
        d = per_day.setdefault((key, local.date()), {"key": key, "progress": 0.0, "completed": False, "ts": local})
        d["progress"] = max(d["progress"], 100.0 if r["action"] == "completed" else float(r.get("progress_pct") or 0))
        d["completed"] |= r["action"] == "completed"

    month_minutes = [0.0] * 12
    hours = [0] * 24
    weekdays = [0] * 7
    days_active: set[date] = set()
    plays: Counter = Counter()
    completions: Counter = Counter()
    series_eps: dict[str, Counter] = defaultdict(Counter)
    genre_minutes: Counter = Counter()
    total = 0.0
    for d in per_day.values():
        t = titles[d["key"]]
        if t["item_type"] == "series":
            continue
        runtime = t["runtime"] or DEFAULT_RUNTIME.get(t["item_type"], 60)
        minutes = runtime * min(d["progress"], 100.0) / 100.0
        if minutes < 3:
            continue
        total += minutes
        month_minutes[d["ts"].month - 1] += minutes
        hours[d["ts"].hour] += 1
        weekdays[d["ts"].weekday()] += 1
        days_active.add(d["ts"].date())
        plays[d["key"]] += 1
        if d["completed"]:
            completions[d["key"]] += 1
            if t["item_type"] == "series_episode" and t["series_id"]:
                series_eps[t["series_id"]][d["ts"].date()] += 1
        for g in t["genres"][:2]:
            genre_minutes[g] += minutes

    def film_rank(k: str) -> float:
        return 3 * max(ratings.get(k, 0), 0) + (2 if k in loved else 0) + completions[k] + 0.3 * plays[k]

    films = [k for k in plays if titles[k]["item_type"] == "movie"]
    top_films = sorted(films, key=film_rank, reverse=True)[:5]
    series_rows = []
    for sid, days in series_eps.items():
        name = next((t["series_name"] for t in titles.values() if t["series_id"] == sid and t["series_name"]), "")
        best_day, best = max(days.items(), key=lambda kv: kv[1])
        series_rows.append({"series_id": sid, "series_name": name, "episodes": sum(days.values()), "best_day": best_day.isoformat(), "best_day_episodes": best})
    series_rows.sort(key=lambda s: (-s["best_day_episodes"], -s["episodes"]))

    streak = best_streak(sorted(days_active))
    gtotal = sum(genre_minutes.values()) or 1
    peak_hour = max(range(24), key=lambda h: hours[h]) if any(hours) else None
    night = sum(hours[h] for h in (22, 23, 0, 1, 2, 3))
    out = {
        "minutes": round(total),
        "films": sum(1 for k in films if completions[k]),
        "episodes": sum(c for k, c in completions.items() if titles[k]["item_type"] == "series_episode"),
        "series": len({titles[k]["series_id"] for k in plays if titles[k]["item_type"] == "series_episode" and titles[k]["series_id"]}),
        "by_month": [round(m) for m in month_minutes],
        "busiest_month": (max(range(12), key=lambda m: month_minutes[m]) + 1) if total else None,
        "by_hour": hours,
        "peak_hour": peak_hour,
        "night_owl_share": round(night / max(1, sum(hours)), 3),
        "by_weekday": weekdays,
        "days_active": len(days_active),
        "longest_streak_days": streak,
        "top_films": [{**{k2: titles[k][k2] for k2 in ("source_item_id", "title")}, "plays": plays[k], "rating": ratings.get(k, 0), "loved": k in loved} for k in top_films],
        "top_series": series_rows[:5],
        "genres": [{"genre": g, "share": round(m / gtotal, 3)} for g, m in genre_minutes.most_common(5)],
        "loved": sum(1 for v in ratings.values() if v == 2) + len(loved),
        "first_watch": min((d["ts"] for d in per_day.values()), default=None),
    }
    if out["first_watch"]:
        out["first_watch"] = out["first_watch"].isoformat()
    if kids:  # kids variant: no time stats
        for k in ("minutes", "by_month", "by_hour", "peak_hour", "night_owl_share", "by_weekday", "busiest_month"):
            out.pop(k, None)
    return out


def best_streak(days: list[date]) -> int:
    best = cur = 0
    prev = None
    for d in days:
        cur = cur + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        best = max(best, cur)
        prev = d
    return best


async def load(pool: asyncpg.Pool, user_id: str, year: int, tz: ZoneInfo) -> list[dict]:
    start = datetime(year, 1, 1, tzinfo=tz).astimezone(timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=tz).astimezone(timezone.utc)
    async with pool.acquire() as conn:
        rows = await conn.fetch(YEAR_SQL, uuid.UUID(user_id), start, end)
    return [dict(r) for r in rows]
