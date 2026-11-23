"""Periodic jobs that talk to the outside world or tidy up.

* weather   — Open-Meteo (no key) for MUSE_WEATHER_LAT/LON -> household 'weather' signal.
* calendars — each person's ICS feeds -> a 'calendar' signal holding only the *kind* of moment
              (workout, work, holiday, travel, dinner, party, date), never titles or attendees.
* webhooks  — deliver queued notifications, HMAC-signed, with retries and backoff.
* digest    — weekly per person: top picks per domain, new releases, a question, goals.
* goals     — progress = matching items completed since the goal started.
* retention — delete events older than each source's retention_policy, old impressions and
              access logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from muse_schema import notify

log = logging.getLogger("muse.scheduler")

WEATHER_LAT = os.environ.get("MUSE_WEATHER_LAT")
WEATHER_LON = os.environ.get("MUSE_WEATHER_LON")
OPEN_METEO = os.environ.get("MUSE_OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast")
REC_URL = os.environ.get("MUSE_REC_URL", "http://recommendation-api:8001").rstrip("/")
REC_READ_KEY = os.environ.get("MUSE_REC_READ_KEY", "")
IMPRESSION_DAYS = int(os.environ.get("MUSE_IMPRESSION_RETENTION_DAYS", "180"))
ACCESS_LOG_DAYS = int(os.environ.get("MUSE_ACCESS_LOG_RETENTION_DAYS", "90"))
BACKOFF_MIN = [1, 5, 30, 120, 720]  # minutes between webhook attempts
MAX_ATTEMPTS = len(BACKOFF_MIN) + 1


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


# --- weather -------------------------------------------------------------------------------------------

def wmo_condition(code: int) -> str:
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 57:
        return "drizzle"
    if 61 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or 85 <= code <= 86:
        return "snow"
    if code >= 95:
        return "thunderstorm"
    return "cloudy"


async def weather(conn, http: httpx.AsyncClient) -> dict | None:
    if not (WEATHER_LAT and WEATHER_LON):
        return None
    r = await http.get(OPEN_METEO, params={"latitude": WEATHER_LAT, "longitude": WEATHER_LON,
                                           "current": "temperature_2m,weather_code,is_day"})
    if r.status_code != 200:
        return None
    cur = r.json().get("current") or {}
    value = {"condition": wmo_condition(int(cur.get("weather_code", 3))), "temp_c": cur.get("temperature_2m"),
             "is_day": bool(cur.get("is_day", 1))}
    await conn.execute("UPDATE context_signal SET expires_at = now() WHERE kind = 'weather' AND user_id IS NULL AND room IS NULL "
                       "AND (expires_at IS NULL OR expires_at > now())")
    await conn.execute("INSERT INTO context_signal (kind, value, source, expires_at) VALUES ('weather', $1::text::jsonb, 'weather', now() + interval '90 minutes')",
                       json.dumps(value))
    return value


# --- calendars ------------------------------------------------------------------------------------------

CAL_KINDS = [
    ("workout", r"\b(gym|workout|training|sport|run|running|hardlopen|fitness|yoga|swim|zwemmen|voetbal|tennis|padel|crossfit)\b"),
    ("holiday", r"\b(holiday|vacation|vakantie|verlof|day off|vrij)\b"),
    ("travel", r"\b(flight|vlucht|train|trein|travel|reis|drive|airport)\b"),
    ("dinner", r"\b(dinner|diner|eten|lunch|brunch)\b"),
    ("party", r"\b(party|feest|borrel|birthday|verjaardag|festival|concert)\b"),
    ("date", r"\b(date|anniversary|jubileum)\b"),
    ("work", r"\b(meeting|standup|stand-up|call|work|werk|deadline|focus|review|sprint|1:1|interview)\b"),
]


def classify(summary: str | None) -> str | None:
    s = (summary or "").lower()
    for kind, pat in CAL_KINDS:
        if re.search(pat, s):
            return kind
    return None


def current_kind(ics: bytes, now: datetime) -> tuple[str | None, datetime | None]:
    """Kind of the event happening now (recurring events expanded), and when it ends."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    best: tuple[str | None, datetime | None] = (None, None)
    for ev in recurring_ical_events.of(cal).between(now - timedelta(hours=12), now + timedelta(minutes=1)):
        start, end = ev.get("DTSTART").dt, (ev.get("DTEND").dt if ev.get("DTEND") else None)
        if not isinstance(start, datetime):  # all-day event: covers the day
            start = datetime(start.year, start.month, start.day, tzinfo=now.tzinfo)
            end = start + timedelta(days=1) if end is None else datetime(end.year, end.month, end.day, tzinfo=now.tzinfo)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = end or start + timedelta(hours=1)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start <= now < end:
            kind = classify(str(ev.get("SUMMARY") or ""))
            if kind and (best[0] is None or kind != "holiday"):  # a specific event beats an all-day holiday
                best = (kind, end)
    return best


async def calendars(conn, http: httpx.AsyncClient, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    n = 0
    for feed in await conn.fetch("SELECT id, user_id, url FROM calendar_feed"):
        try:
            r = await http.get(feed["url"])
            r.raise_for_status()
            kind, until = current_kind(r.content, now)
            await conn.execute("UPDATE calendar_feed SET last_fetched_at = now(), last_error = NULL WHERE id = $1", feed["id"])
        except Exception as e:
            await conn.execute("UPDATE calendar_feed SET last_fetched_at = now(), last_error = $2 WHERE id = $1", feed["id"], str(e)[:300])
            continue
        await conn.execute("UPDATE context_signal SET expires_at = now() WHERE kind = 'calendar' AND user_id = $1 AND source = 'calendar' "
                           "AND (expires_at IS NULL OR expires_at > now())", feed["user_id"])
        if kind:
            await conn.execute("INSERT INTO context_signal (user_id, kind, value, source, expires_at) VALUES ($1, 'calendar', $2::text::jsonb, 'calendar', $3)",
                               feed["user_id"], json.dumps({"kind": kind}), until)
            n += 1
    return n


# --- webhooks ---------------------------------------------------------------------------------------------

def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(conn, http: httpx.AsyncClient, limit: int = 100) -> dict:
    rows = await conn.fetch(
        "SELECT d.id, d.event, d.payload, d.attempts, w.id AS hook, w.url, w.secret FROM webhook_delivery d JOIN webhook w ON w.id = d.webhook_id "
        "WHERE d.delivered_at IS NULL AND d.next_attempt_at <= now() AND d.attempts < $1 AND w.active ORDER BY d.created_at LIMIT $2",
        MAX_ATTEMPTS, limit)
    stats = {"delivered": 0, "failed": 0}
    for r in rows:
        body = json.dumps({"id": str(r["id"]), "event": r["event"], "data": _j(r["payload"]),
                           "sent_at": datetime.now(timezone.utc).isoformat()}, default=str).encode()
        try:
            resp = await http.post(r["url"], content=body, headers={
                "Content-Type": "application/json", "X-Muse-Event": r["event"], "X-Muse-Delivery": str(r["id"]),
                "X-Muse-Signature": signature(r["secret"], body)})
            ok = 200 <= resp.status_code < 300
            err = None if ok else f"HTTP {resp.status_code}"
        except httpx.HTTPError as e:
            ok, err = False, str(e)[:300]
        if ok:
            await conn.execute("UPDATE webhook_delivery SET delivered_at = now(), attempts = attempts + 1 WHERE id = $1", r["id"])
            await conn.execute("UPDATE webhook SET failures = 0 WHERE id = $1", r["hook"])
            stats["delivered"] += 1
        else:
            wait = BACKOFF_MIN[min(r["attempts"], len(BACKOFF_MIN) - 1)]
            await conn.execute("UPDATE webhook_delivery SET attempts = attempts + 1, last_error = $2, next_attempt_at = now() + make_interval(mins => $3) "
                               "WHERE id = $1", r["id"], err, wait)
            await conn.execute("UPDATE webhook SET failures = failures + 1, active = (failures + 1 < 50) WHERE id = $1", r["hook"])
            stats["failed"] += 1
    return stats


# --- weekly digest -----------------------------------------------------------------------------------------

async def _rec(http: httpx.AsyncClient, user: str, item_type: str, limit: int = 5) -> list[dict]:
    headers = {"X-Muse-Read-Key": REC_READ_KEY} if REC_READ_KEY else {}
    try:
        r = await http.get(f"{REC_URL}/recommendations/{user}", params={"item_type": item_type, "limit": limit, "surface": "digest"}, headers=headers)
        return r.json().get("recommendations", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []


async def digest(conn, http: httpx.AsyncClient, now: datetime | None = None, force: bool = False) -> int:
    """Weekly, for people who have a webhook listening to 'digest' (or a household hook)."""
    now = now or datetime.now(timezone.utc)
    week = now.strftime("%G-W%V")
    if not force and now.weekday() != int(os.environ.get("MUSE_DIGEST_WEEKDAY", "0")):
        return 0
    if not await conn.fetchval("SELECT 1 FROM webhook WHERE active AND ('digest' = ANY(events) OR '*' = ANY(events)) LIMIT 1"):
        return 0
    people = await conn.fetch(
        "SELECT COALESCE(a.user_id, e.user_id) AS person, array_agg(DISTINCT e.item_type) AS types FROM event e "
        "LEFT JOIN user_alias a ON a.alias_id = e.user_id WHERE e.\"timestamp\" > now() - interval '30 days' AND e.action <> 'cataloged' "
        "AND e.action_weight > 0 GROUP BY 1")
    sent = 0
    for p in people:
        fresh = await conn.fetchval("INSERT INTO notification_log (key) VALUES ($1) ON CONFLICT DO NOTHING RETURNING key", f"digest:{p['person']}:{week}")
        if fresh is None:
            continue
        picks = {}
        for t in [x for x in p["types"] if x in ("track", "movie", "series", "youtube_video", "audiobook", "ebook")][:4]:
            picks[t] = [{"title": r.get("title"), "source": r["source"], "source_item_id": r["source_item_id"], "why": (r.get("why") or [None])[0]}
                        for r in await _rec(http, str(p["person"]), t)]
        releases = [dict(artist=r["artist_name"], title=r["title"], date=str(r["release_date"]) if r["release_date"] else None) for r in await conn.fetch(
            "SELECT artist_name, title, release_date FROM wanted WHERE user_id = $1 AND origin = 'new_release' AND created_at > now() - interval '7 days'",
            p["person"])]
        question = await conn.fetchval("SELECT prompt FROM question WHERE user_id = $1 AND status = 'open' ORDER BY priority DESC LIMIT 1", p["person"])
        goals = [dict(title=g["title"], progress=g["progress"], target=g["target"]) for g in await conn.fetch(
            "SELECT title, progress, target FROM goal WHERE user_id = $1 AND status = 'active'", p["person"])]
        sent += await notify.emit(conn, "digest", {"week": week, "picks": picks, "new_releases": releases, "question": question, "goals": goals},
                                  user_id=p["person"]) > 0
    return sent


# --- goals ---------------------------------------------------------------------------------------------------

async def goals(conn) -> int:
    """Progress: distinct items completed (or favourited/rated up) since the goal started that match it
    (genre/artist criteria, or the goal's text vector via the content embedding's genres)."""
    changed = 0
    for g in await conn.fetch("SELECT * FROM goal WHERE status = 'active'"):
        c = _j(g["criteria"]) or {}
        genres = [x.lower() for x in c.get("genres", [])]
        artists = [x.lower() for x in c.get("artists", [])]
        items = c.get("items", [])  # explicit "source:id" list (e.g. a list of classics)
        rows = await conn.fetch(
            """
            SELECT DISTINCT e.source, e.source_item_id, e.metadata FROM event e LEFT JOIN user_alias a ON a.alias_id = e.user_id
            WHERE COALESCE(a.user_id, e.user_id) = $1 AND e."timestamp" >= $2 AND e.action IN ('completed', 'favorited', 'rated', 'added_to_playlist')
              AND e.action_weight > 0 AND ($3 = 'general' OR e.item_type = $3)
            """, g["user_id"], g["created_at"], g["domain"])
        hit = set()
        meta_by = {}
        for r in rows:
            meta_by[(r["source"], r["source_item_id"])] = _j(r["metadata"]) or {}
        for key, md in meta_by.items():
            if not md:
                md = _j(await conn.fetchval("SELECT metadata FROM event WHERE source = $1 AND source_item_id = $2 AND metadata <> '{}'::jsonb LIMIT 1", *key)) or {}
            g_ok = genres and {x.lower() for x in md.get("genres", []) or []} & set(genres)
            a_ok = artists and (md.get("artist") or "").lower() in artists
            i_ok = f"{key[0]}:{key[1]}" in items
            if g_ok or a_ok or i_ok:
                hit.add(key)
        progress = len(hit)
        if progress != g["progress"]:
            done = progress >= g["target"]
            await conn.execute("UPDATE goal SET progress = $2, status = CASE WHEN $3 THEN 'done' ELSE status END, done_at = CASE WHEN $3 THEN now() ELSE done_at END WHERE id = $1",
                               g["id"], progress, done)
            await notify.emit(conn, "goal", {"id": str(g["id"]), "title": g["title"], "progress": progress, "target": g["target"], "done": done},
                              user_id=g["user_id"], key=f"goal:{g['id']}:{progress}")
            changed += 1
    return changed


# --- retention -------------------------------------------------------------------------------------------------

async def retention(conn) -> dict:
    out = {}
    for p in await conn.fetch("SELECT source, keep_days FROM retention_policy"):
        res = await conn.execute("DELETE FROM event WHERE source = $1 AND \"timestamp\" < now() - make_interval(days => $2)", p["source"], p["keep_days"])
        out[p["source"]] = int(res.split()[-1])
    res = await conn.execute("DELETE FROM engine_impression WHERE shown_at < now() - make_interval(days => $1)", IMPRESSION_DAYS)
    out["impressions"] = int(res.split()[-1])
    res = await conn.execute("DELETE FROM access_log WHERE at < now() - make_interval(days => $1)", ACCESS_LOG_DAYS)
    out["access_log"] = int(res.split()[-1])
    await conn.execute("DELETE FROM context_signal WHERE expires_at < now() - interval '7 days'")
    await conn.execute("DELETE FROM webhook_delivery WHERE delivered_at < now() - interval '30 days'")
    return out
