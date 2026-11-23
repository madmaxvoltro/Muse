"""Catalog worker: gives Muse music that exists, not only music that's in the library.

Each cycle (MUSE_CATALOG_INTERVAL_SECONDS, default 30 min), within per-cycle budgets so the
public APIs are treated politely:

1. **Seeds** — the artists each person loves (net positive weight in the last year), resolved
   to MusicBrainz ids. seed_weight = how much the household loves them.
2. **Enrich** — genres/tags (MusicBrainz), related artists (ListenBrainz listening similarity,
   Deezer as fallback), Deezer ids.
3. **Tracks** — top tracks of seeds and their related artists (Deezer, with 30 s previews,
   cross-referenced with ListenBrainz' most-listened recordings for MusicBrainz ids) become
   `cataloged` events with source "catalog". The embedding worker embeds them, the audio
   analyzer listens to the previews, and the recommendation API can recommend them
   (`source=catalog`): discovery beyond what you own.
4. **New releases** — release groups of loved artists; ones that are new (or recent on first
   sight) become `wanted` rows for each person who loves the artist, plus a `new_release`
   webhook.
5. **Wanted** — the best catalog recommendations per person (discovery) and Mono wishlist songs
   join the wanted list a music-Radarr / Lidarr pulls; rows flip to in_library when the music
   shows up in Mono.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg
from muse_schema.config import database_url
from muse_schema.storage import fetch_open, insert_event, item_ids
import httpx
from muse_schema import notify
from muse_schema.config import secret

from .clients import Deezer, Http, ListenBrainz, MusicBrainz

log = logging.getLogger("muse.catalog")

CATALOG_USER = uuid.UUID("00000000-0000-0000-0000-00000000ca7a")  # owner of catalog events (no person)
LIBRARY_SOURCES = ("mono", "navidrome", "jellyfin")
SKIP_SECONDARY = {"compilation", "live", "remix", "dj-mix", "mixtape/street", "demo", "interview", "audiobook", "spokenword"}

INTERVAL = int(os.environ.get("MUSE_CATALOG_INTERVAL_SECONDS", "1800"))
RESOLVE_PER_CYCLE = int(os.environ.get("MUSE_CATALOG_RESOLVE_PER_CYCLE", "60"))
ENRICH_PER_CYCLE = int(os.environ.get("MUSE_CATALOG_ENRICH_PER_CYCLE", "15"))
TRACK_ARTISTS_PER_CYCLE = int(os.environ.get("MUSE_CATALOG_TRACK_ARTISTS_PER_CYCLE", "25"))
TRACKS_PER_ARTIST = int(os.environ.get("MUSE_CATALOG_TRACKS_PER_ARTIST", "8"))
RELATED_PER_SEED = int(os.environ.get("MUSE_CATALOG_RELATED_PER_SEED", "8"))
RELEASES_PER_CYCLE = int(os.environ.get("MUSE_CATALOG_RELEASES_PER_CYCLE", "20"))
NEW_RELEASE_WINDOW_DAYS = int(os.environ.get("MUSE_NEW_RELEASE_WINDOW_DAYS", "90"))
DISCOVERY_PER_PERSON = int(os.environ.get("MUSE_DISCOVERY_WANTED_PER_DAY", "5"))
DISCOVERY_MIN_SCORE = float(os.environ.get("MUSE_DISCOVERY_MIN_SCORE", "0.45"))
REC_URL = os.environ.get("MUSE_REC_URL", "http://recommendation-api:8001").rstrip("/")
REC_READ_KEY = secret("MUSE_REC_READ_KEY", "") or ""


# --- names --------------------------------------------------------------------------------------

_FEAT = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring|with)\b.*$", re.I)
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*\b(remaster(ed)?|live|version|edit|mix|mono|stereo|deluxe|explicit)\b[^\)\]]*[\)\]]", re.I)


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", s)


def primary_artist(s: str | None) -> str:
    """'Kensington feat. X' -> 'Kensington'; 'A, B & C' stays (commas are part of some names)."""
    return _FEAT.sub("", s or "").strip()


def track_key(artist: str | None, title: str | None) -> str:
    title = title or ""
    if artist and title.casefold().startswith((artist + " - ").casefold()):
        title = title[len(artist) + 3:]
    title = _FEAT.sub("", _BRACKETS.sub("", title))
    return f"{norm(primary_artist(artist))}|{norm(title)}"


def _j(v: Any) -> Any:
    """jsonb as read with or without a json codec."""
    return json.loads(v) if isinstance(v, str) else v


def _date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt, n in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


# --- 1. seeds -------------------------------------------------------------------------------------


async def loved_artists(conn, days: int = 365) -> dict[uuid.UUID, dict[str, float]]:
    """person -> {artist name: net weight}, only net-positive artists."""
    # artists live in the sealed metadata (muse_schema.storage): sum in Python
    sums: dict[tuple[uuid.UUID, str], float] = defaultdict(float)
    for r in await fetch_open(
        conn,
        """
        SELECT COALESCE(a.user_id, e.user_id) AS person, e.source, e.source_item_id, e.metadata, e.action_weight AS w
        FROM event e LEFT JOIN user_alias a ON a.alias_id = e.user_id
        WHERE e.item_type = 'track' AND e.action <> 'cataloged' AND e.source <> 'catalog'
          AND e."timestamp" > now() - make_interval(days => $1::int)
        """,
        days,
    ):
        artist = (r["metadata"] or {}).get("artist")
        if artist:
            sums[(r["person"], artist)] += float(r["w"])
    out: dict[uuid.UUID, dict[str, float]] = defaultdict(dict)
    for (person, artist), w in sums.items():
        name = primary_artist(artist)
        if w > 0 and name:
            out[person][name] = out[person].get(name, 0.0) + w
    return out


async def resolve(conn, mb: MusicBrainz, name: str) -> str | None:
    key = norm(name)
    if not key:
        return None
    cached = await conn.fetchrow("SELECT mbid FROM catalog_name WHERE name_key = $1", key)
    if cached is not None:
        return cached["mbid"]
    hits = await mb.search_artist(name)
    exact = [h for h in hits if norm(h["name"]) == key and h["score"] >= 90]
    mbid = exact[0]["mbid"] if exact else (hits[0]["mbid"] if hits and hits[0]["score"] == 100 else None)
    await conn.execute(
        "INSERT INTO catalog_name (name_key, mbid) VALUES ($1, $2) ON CONFLICT (name_key) DO UPDATE SET mbid = $2, resolved_at = now()",
        key, mbid,
    )
    return mbid


async def refresh_seeds(conn, mb: MusicBrainz, loved: dict[uuid.UUID, dict[str, float]]) -> int:
    household: dict[str, float] = defaultdict(float)
    for artists in loved.values():
        top = max(artists.values(), default=1.0) or 1.0
        for name, w in artists.items():
            household[name] += w / top  # each person counts equally, whatever their volume
    resolved = 0
    seeds: dict[str, tuple[str, float]] = {}
    for name, w in sorted(household.items(), key=lambda x: -x[1]):
        cached = await conn.fetchrow("SELECT mbid FROM catalog_name WHERE name_key = $1", norm(name))
        if cached is None:
            if resolved >= RESOLVE_PER_CYCLE:
                continue
            resolved += 1
            mbid = await resolve(conn, mb, name)
        else:
            mbid = cached["mbid"]
        if mbid:
            prev = seeds.get(mbid)
            seeds[mbid] = (name, w + (prev[1] if prev else 0.0))
    await conn.execute("UPDATE catalog_artist SET seed_weight = 0 WHERE seed_weight > 0 AND NOT (mbid = ANY($1::text[]))", list(seeds))
    for mbid, (name, w) in seeds.items():
        await conn.execute(
            "INSERT INTO catalog_artist (mbid, name, seed_weight) VALUES ($1, $2, $3) "
            "ON CONFLICT (mbid) DO UPDATE SET seed_weight = $3",
            mbid, name, round(w, 4),
        )
    return len(seeds)


# --- 2. enrich ---------------------------------------------------------------------------------


async def enrich(conn, mb: MusicBrainz, lb: ListenBrainz, dz: Deezer) -> int:
    rows = await conn.fetch(
        "SELECT mbid, name, deezer_id FROM catalog_artist WHERE seed_weight > 0 "
        "AND (fetched_at IS NULL OR fetched_at < now() - interval '14 days') ORDER BY seed_weight DESC LIMIT $1",
        ENRICH_PER_CYCLE,
    )
    for r in rows:
        info = await mb.artist(r["mbid"]) or {}
        dz_id = r["deezer_id"]
        if dz_id is None:
            hit = await dz.search_artist(r["name"])
            dz_id = hit["id"] if hit and norm(hit["name"]) == norm(r["name"]) else None
        related = await lb.similar_artists(r["mbid"], 25)
        if len(related) < 5 and dz_id:
            seen = {norm(x["name"]) for x in related}
            related += [x for x in await dz.related(dz_id) if norm(x["name"]) not in seen]
        for x in related[:RELATED_PER_SEED * 2]:
            if not x.get("mbid"):
                x["mbid"] = await resolve(conn, mb, x["name"])
            if x.get("mbid"):
                await conn.execute(
                    "INSERT INTO catalog_artist (mbid, name, deezer_id) VALUES ($1, $2, $3) ON CONFLICT (mbid) DO "
                    "UPDATE SET deezer_id = COALESCE(catalog_artist.deezer_id, EXCLUDED.deezer_id)",
                    x["mbid"], x["name"], x.get("deezer_id"),
                )
        await conn.execute(
            "UPDATE catalog_artist SET tags = $2::text::jsonb, country = COALESCE($3, country), deezer_id = $4, "
            "related = $5::text::jsonb, fetched_at = now() WHERE mbid = $1",
            r["mbid"], json.dumps(info.get("tags", [])), info.get("country"), dz_id, json.dumps(related),
        )
    return len(rows)


# --- 3. tracks -----------------------------------------------------------------------------------


async def library_keys(conn) -> set[str]:
    rows = await _library_tracks(conn)
    return {track_key(md.get("artist"), md.get("title")) for md in rows}


async def _library_tracks(conn) -> list[dict]:
    """Metadata of every track in a library (opened, see muse_schema.storage)."""
    rows = await fetch_open(
        conn,
        """
        SELECT DISTINCT ON (source, source_item_id) source, source_item_id, metadata FROM event
        WHERE item_type = 'track' AND source = ANY($1::text[]) AND action = 'cataloged'
        ORDER BY source, source_item_id, "timestamp" DESC
        """,
        list(LIBRARY_SOURCES),
    )
    return [r["metadata"] for r in rows if r["metadata"] and str(r["metadata"].get("in_library", True)).lower() != "false"]


async def _tracked_artists(conn) -> list[dict]:
    """Seeds by weight, interleaved with their related artists: what to fetch tracks for."""
    seeds = await conn.fetch("SELECT mbid, name, deezer_id, related, tags FROM catalog_artist WHERE seed_weight > 0 ORDER BY seed_weight DESC")
    plan: list[dict] = []
    seen: set[str] = set()
    for s in seeds:
        if s["mbid"] not in seen:
            seen.add(s["mbid"])
            plan.append({"mbid": s["mbid"], "name": s["name"], "deezer_id": s["deezer_id"], "related_to": None})
        for rel in (_j(s["related"]) or [])[:RELATED_PER_SEED]:
            if rel.get("mbid") and rel["mbid"] not in seen:
                seen.add(rel["mbid"])
                plan.append({"mbid": rel["mbid"], "name": rel["name"], "deezer_id": rel.get("deezer_id"), "related_to": s["name"]})
    return plan


async def fetch_tracks(conn, lb: ListenBrainz, dz: Deezer, mb: MusicBrainz) -> int:
    plan = await _tracked_artists(conn)
    due = {r["mbid"] for r in await conn.fetch(
        "SELECT mbid FROM catalog_artist WHERE mbid = ANY($1::text[]) AND (tracks_fetched_at IS NULL OR tracks_fetched_at < now() - interval '30 days')",
        [p["mbid"] for p in plan],
    )}
    plan = [p for p in plan if p["mbid"] in due][:TRACK_ARTISTS_PER_CYCLE]
    have = await library_keys(conn)
    emitted = 0
    for p in plan:
        tags = _j(await conn.fetchval("SELECT tags FROM catalog_artist WHERE mbid = $1", p["mbid"])) or []
        dz_id = p["deezer_id"]
        if dz_id is None:
            hit = await dz.search_artist(p["name"])
            dz_id = hit["id"] if hit and norm(hit["name"]) == norm(p["name"]) else None
        lb_top = {norm(r["title"]): r for r in await lb.top_recordings(p["mbid"], 25)}
        tracks = await dz.top_tracks(dz_id, TRACKS_PER_ARTIST) if dz_id else []
        if not tracks:  # no Deezer: ListenBrainz alone (no previews)
            tracks = [{"title": r["title"], "artist": r["artist"] or p["name"], "album": r["album"], "recording_mbid": r["recording_mbid"],
                       "listens": r["listens"]} for r in list(lb_top.values())[:TRACKS_PER_ARTIST]]
        for t in tracks:
            artist = t.get("artist") or p["name"]
            if track_key(artist, t["title"]) in have:
                continue
            lbr = lb_top.get(norm(t["title"]))
            rec_mbid = t.get("recording_mbid") or (lbr or {}).get("recording_mbid")
            sid = f"mb:{rec_mbid}" if rec_mbid else f"dz:{t['deezer_id']}"
            extra = await dz.track(t["deezer_id"]) if t.get("deezer_id") else None
            why = f"related to {p['related_to']}, which you love" if p["related_to"] else f"by {p['name']}, whom you love"
            md = {
                "title": f"{artist} - {t['title']}", "artist": artist, "album": t.get("album"),
                "genres": list(tags[:6]),
                "description": f"Song by {artist}" + (f" from {t['album']}" if t.get("album") else "") + f"; {why}."
                               + (f" Tags: {', '.join(tags[:6])}." if tags else ""),
                "in_library": False, "artist_mbid": p["mbid"], "recording_mbid": rec_mbid,
                "deezer_id": t.get("deezer_id"), "preview_url": (extra or {}).get("preview_url") or t.get("preview_url"),
                "isrc": (extra or {}).get("isrc"), "bpm": (extra or {}).get("bpm"), "duration_s": t.get("duration_s"),
                "popularity": t.get("rank") or (lbr or {}).get("listens"), "related_to": p["related_to"],
                "dedupe_key": f"catalog:{sid}",
            }
            md = {k: v for k, v in md.items() if v not in (None, "", [])}
            # once per catalog track: the dedupe key (catalog:<sid>) makes the insert idempotent
            inserted = await insert_event(conn, user_id=CATALOG_USER, source="catalog", source_item_id=sid, item_type="track",
                                          action="cataloged", action_weight=0.0, metadata=md)
            if inserted:
                emitted += 1
                if md.get("preview_url"):
                    await conn.execute("INSERT INTO item_audio (source, source_item_id) VALUES ('catalog', $1) ON CONFLICT DO NOTHING", sid)
        await conn.execute("UPDATE catalog_artist SET tracks_fetched_at = now(), deezer_id = COALESCE(deezer_id, $2) WHERE mbid = $1", p["mbid"], dz_id)
    return emitted


# --- 4. new releases -----------------------------------------------------------------------------


async def check_releases(conn, mb: MusicBrainz, loved: dict[uuid.UUID, dict[str, float]]) -> int:
    rows = await conn.fetch(
        "SELECT mbid, name, releases_checked_at FROM catalog_artist WHERE seed_weight > 0 "
        "AND (releases_checked_at IS NULL OR releases_checked_at < now() - interval '20 hours') ORDER BY seed_weight DESC LIMIT $1",
        RELEASES_PER_CYCLE,
    )
    fans: dict[str, list[tuple[uuid.UUID, float]]] = defaultdict(list)
    for person, artists in loved.items():
        top = max(artists.values(), default=1.0) or 1.0
        for name, w in artists.items():
            fans[norm(name)].append((person, w / top))
    today = datetime.now(timezone.utc).date()
    new = 0
    for r in rows:
        groups = await mb.release_groups(r["mbid"])
        known = {x["mbid"] for x in await conn.fetch("SELECT mbid FROM catalog_release WHERE artist_mbid = $1", r["mbid"])}
        first_look = r["releases_checked_at"] is None
        for g in groups:
            if g["mbid"] in known:
                continue
            d = _date(g["date"])
            await conn.execute(
                "INSERT INTO catalog_release (mbid, artist_mbid, artist_name, title, primary_type, secondary_types, release_date) "
                "VALUES ($1, $2, $3, $4, $5, $6::text::jsonb, $7) ON CONFLICT (mbid) DO NOTHING",
                g["mbid"], r["mbid"], r["name"], g["title"], g["primary_type"], json.dumps(g["secondary_types"]), d,
            )
            if {s.lower() for s in g["secondary_types"]} & SKIP_SECONDARY:
                continue
            recent = d is not None and d >= today - timedelta(days=NEW_RELEASE_WINDOW_DAYS)
            if first_look and not recent:
                continue  # back catalogue, seen for the first time: not "new"
            if not first_look and d is not None and not recent:
                continue  # newly added to MusicBrainz but old
            new += 1
            upcoming = d is not None and d > today
            for person, affinity in fans.get(norm(r["name"]), []):
                await add_wanted(
                    conn, person, kind="release", mbid=g["mbid"], artist_mbid=r["mbid"], artist_name=r["name"], title=g["title"],
                    release_date=d, origin="new_release", score=round(0.5 + 0.5 * affinity, 3),
                    reason=f"{'Upcoming' if upcoming else 'New'} {(g['primary_type'] or 'release').lower()} by {r['name']}",
                    dedupe_key=f"release:{g['mbid']}",
                )
                await notify.emit(conn, "new_release", {
                    "artist": r["name"], "artist_mbid": r["mbid"], "title": g["title"], "release_group_mbid": g["mbid"],
                    "type": g["primary_type"], "release_date": str(d) if d else None, "upcoming": upcoming,
                }, user_id=person, key=f"new_release:{g['mbid']}:{person}")
        await conn.execute("UPDATE catalog_artist SET releases_checked_at = now() WHERE mbid = $1", r["mbid"])
    return new


# --- 5. wanted -----------------------------------------------------------------------------------


async def add_wanted(conn, user_id: uuid.UUID | None, *, kind: str, artist_name: str, origin: str, dedupe_key: str,
                     mbid: str | None = None, artist_mbid: str | None = None, title: str | None = None,
                     release_date: date | None = None, reason: str | None = None, score: float = 0.5) -> bool:
    row = await conn.fetchrow(
        """
        INSERT INTO wanted (user_id, kind, mbid, artist_mbid, artist_name, title, release_date, reason, score, origin, dedupe_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (user_id, dedupe_key) DO NOTHING RETURNING id
        """,
        user_id, kind, mbid, artist_mbid, artist_name, title, release_date, reason, score, origin, dedupe_key,
    )
    if row:
        await notify.emit(conn, "wanted", {
            "id": str(row["id"]), "kind": kind, "mbid": mbid, "artist_mbid": artist_mbid, "artist": artist_name, "title": title,
            "origin": origin, "reason": reason, "score": score,
        }, user_id=user_id, key=f"wanted:{row['id']}")
    return row is not None


async def discovery_wanted(conn, persons: list[uuid.UUID], rec_get=None) -> int:
    rec_get = rec_get or _rec_get
    added = 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for person in persons:
        fresh = await conn.fetchval(
            "INSERT INTO notification_log (key) VALUES ($1) ON CONFLICT DO NOTHING RETURNING key", f"discovery:{person}:{stamp}"
        )
        if fresh is None:
            continue
        recs = await rec_get(str(person), {"item_type": "track", "source": "catalog", "limit": 30})
        picks = [r for r in recs if r.get("score", 0) >= DISCOVERY_MIN_SCORE][:DISCOVERY_PER_PERSON]
        for r in picks:
            md = next((e["metadata"] for e in await fetch_open(
                conn, "SELECT source, source_item_id, metadata FROM event WHERE source = 'catalog' AND source_item_id = ANY($1::text[]) "
                      "AND action = 'cataloged' LIMIT 1", item_ids("catalog", r["source_item_id"]))), {})
            title = md.get("title", r.get("title", ""))
            artist = md.get("artist") or title.split(" - ")[0]
            added += await add_wanted(
                conn, person, kind="recording", mbid=md.get("recording_mbid"), artist_mbid=md.get("artist_mbid"),
                artist_name=artist, title=title.split(" - ", 1)[-1], origin="discovery", score=round(float(r["score"]), 3),
                reason=(r.get("why") or ["Muse thinks you'll like this"])[0], dedupe_key=f"rec:{r['source_item_id']}",
            )
    return added


async def wishlist_wanted(conn) -> int:
    rows = await fetch_open(
        conn,
        """
        SELECT DISTINCT ON (COALESCE(a.user_id, e.user_id), e.source_item_id)
               COALESCE(a.user_id, e.user_id) AS person, e.source, e.source_item_id, e.metadata
        FROM event e LEFT JOIN user_alias a ON a.alias_id = e.user_id
        WHERE e.source = 'mono' AND e.action IN ('favorited', 'added_to_playlist')
        """
    )
    added = 0
    for r in rows:
        if not str(r["source_item_id"] or "").startswith("wish:"):  # ids are blind in SQL; checked once opened
            continue
        md = _j(r["metadata"]) or {}
        title = md.get("title", "")
        artist = md.get("artist") or title.split(" - ")[0]
        added += await add_wanted(conn, r["person"], kind="recording", artist_name=artist, title=title.split(" - ", 1)[-1],
                                  origin="wishlist", score=0.8, reason="On your wishlist (saved on another service)",
                                  dedupe_key=f"wish:{r['source_item_id']}")
    return added


async def mark_in_library(conn) -> int:
    have = await library_keys(conn)
    albums = {(norm(primary_artist(md.get("artist"))), norm(md["album"])) for md in await _library_tracks(conn) if md.get("album")}
    n = 0
    for w in await conn.fetch("SELECT id, kind, artist_name, title FROM wanted WHERE status IN ('wanted', 'grabbed')"):
        hit = (w["kind"] == "recording" and track_key(w["artist_name"], w["title"]) in have) or \
              (w["kind"] == "release" and (norm(primary_artist(w["artist_name"])), norm(w["title"])) in albums)
        if hit:
            await conn.execute("UPDATE wanted SET status = 'in_library', updated_at = now() WHERE id = $1", w["id"])
            n += 1
    return n


async def _rec_get(user_id: str, params: dict) -> list[dict]:
    headers = {"X-Muse-Read-Key": REC_READ_KEY} if REC_READ_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{REC_URL}/recommendations/{user_id}", params=params, headers=headers)
        return r.json().get("recommendations", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []


# --- cycle -------------------------------------------------------------------------------------------


async def run_cycle(pool: asyncpg.Pool, mb: MusicBrainz, lb: ListenBrainz, dz: Deezer, rec_get=None) -> dict[str, int]:
    stats: dict[str, int] = {}
    async with pool.acquire() as conn:
        loved = await loved_artists(conn)
        stats["seeds"] = await refresh_seeds(conn, mb, loved)
        stats["enriched"] = await enrich(conn, mb, lb, dz)
        stats["tracks"] = await fetch_tracks(conn, lb, dz, mb)
        stats["new_releases"] = await check_releases(conn, mb, loved)
        stats["discovery"] = await discovery_wanted(conn, list(loved), rec_get)
        stats["wishlist"] = await wishlist_wanted(conn)
        stats["in_library"] = await mark_in_library(conn)
    log.info("catalog cycle: %s", stats)
    return stats


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=3)
    http = Http()
    mb, lb, dz = MusicBrainz(http), ListenBrainz(http), Deezer(http)
    while True:
        try:
            await run_cycle(pool, mb, lb, dz)
        except Exception:
            log.exception("catalog cycle failed")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
