"""Navidrome adapter (Subsonic API). A poller, like the Jellyfin adapter.

* Catalog (daily): every song via search3 paging -> `cataloged` with artist, album, year,
  genre, duration, so Muse can embed (and the audio analyzer can listen to) the library.
* Now playing (every 20 s, admin credentials): getNowPlaying lists every user's current song.
  A song seen for a user -> `played`; when it is replaced, how long it ran decides `completed`
  (>= 80% of its duration, or the last 30 s) or `skipped` (< 50%). Device = the player name.
* Stars (every 10 min, per user, optional NAVIDROME_USERS credentials): starred songs ->
  `favorited`, unstarred later -> `unfavorited`.

Identity: Muse user id = uuid5(namespace, "navidrome:<username>"); link it to the person's
other accounts with a link code. Events carry dedupe keys; state is in memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("muse.navidrome_adapter")

ND_URL = os.environ.get("NAVIDROME_URL", "").rstrip("/")
ND_USER = os.environ.get("NAVIDROME_USER", "")
ND_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")
# Optional "alice:pw,bob:pw" for per-user stars (Subsonic answers starred songs per account).
ND_USERS = [tuple(p.split(":", 1)) for p in os.environ.get("NAVIDROME_USERS", "").split(",") if ":" in p]
INGESTION_URL = os.environ.get("MUSE_INGESTION_URL", "").rstrip("/")
ADAPTER_KEY = os.environ.get("MUSE_ADAPTER_KEY", "")
NOW_PLAYING_SECONDS = int(os.environ.get("MUSE_NOW_PLAYING_INTERVAL_SECONDS", "20"))
STARS_SECONDS = int(os.environ.get("MUSE_STARS_INTERVAL_SECONDS", "600"))
CATALOG_SECONDS = int(os.environ.get("MUSE_CATALOG_SYNC_INTERVAL_SECONDS", "86400"))
NS = uuid.UUID("9f4c2a51-6a3e-4a8e-9c55-1c3b6d0e2b77")


def muse_user(username: str) -> str:
    return str(uuid.uuid5(NS, f"navidrome:{username.casefold()}"))


def auth(user: str, password: str) -> dict:
    salt = secrets.token_hex(6)
    return {"u": user, "t": hashlib.md5((password + salt).encode()).hexdigest(), "s": salt, "v": "1.16.1", "c": "muse", "f": "json"}


async def sub(client: httpx.AsyncClient, endpoint: str, user: str = "", password: str = "", **params) -> dict:
    r = await client.get(f"{ND_URL}/rest/{endpoint}", params={**auth(user or ND_USER, password or ND_PASSWORD), **params})
    r.raise_for_status()
    body = r.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise RuntimeError(f"{endpoint}: {body.get('error')}")
    return body


def song_md(s: dict) -> dict:
    artist = s.get("artist") or s.get("displayArtist") or ""
    md = {"title": f"{artist} - {s.get('title', '')}" if artist else s.get("title", ""), "artist": artist, "album": s.get("album"),
          "year": s.get("year"), "genres": [g["name"] for g in s.get("genres", []) if g.get("name")] or ([s["genre"]] if s.get("genre") else []),
          "duration_s": s.get("duration"), "description": f"Song by {artist}" + (f" from the album {s['album']}" if s.get("album") else "") + "."}
    return {k: v for k, v in md.items() if v not in (None, "", [])}


async def post(client: httpx.AsyncClient, event: dict) -> bool:
    try:
        r = await client.post(f"{INGESTION_URL}/events", json=event, headers={"X-Muse-Api-Key": ADAPTER_KEY})
        return r.status_code < 300 or (400 <= r.status_code < 500 and r.status_code != 429)
    except httpx.HTTPError as e:
        log.warning("posting failed: %s", e)
        return False


def event(username: str, song_id: str, action: str, md: dict, *, dedupe: str | None = None, progress: float | None = None,
          device: str | None = None, at: datetime | None = None) -> dict:
    e = {"user_id": muse_user(username), "source": "navidrome", "source_item_id": song_id, "item_type": "track", "action": action,
         "timestamp": (at or datetime.now(timezone.utc)).isoformat(), "metadata": {**md, **({"dedupe_key": dedupe} if dedupe else {})}}
    if progress is not None:
        e["progress_pct"] = round(max(0.0, min(100.0, progress)), 1)
    if device:
        e["device_context"] = {"device": device}
    return e


class NowPlaying:
    """Turns getNowPlaying snapshots into played / completed / skipped."""

    def __init__(self) -> None:
        self.current: dict[str, dict] = {}  # username -> {id, started (monotonic), duration, md, device}

    def update(self, entries: list[dict], now: float) -> list[dict]:
        out = []
        seen = {}
        for e in entries:
            user = e.get("username")
            if not user or not e.get("id"):
                continue
            started = now - 60 * float(e.get("minutesAgo", 0) or 0)
            seen[user] = {"id": e["id"], "started": started, "duration": float(e.get("duration") or 0), "md": song_md(e), "device": e.get("playerName")}
        for user, cur in list(self.current.items()):
            nxt = seen.get(user)
            if nxt and nxt["id"] == cur["id"]:
                continue
            ran = now - cur["started"]
            dur = cur["duration"] or 0
            pct = 100 * ran / dur if dur else None
            if dur and (ran >= 0.8 * dur or dur - ran <= 30):
                out.append(event(user, cur["id"], "completed", cur["md"], progress=100, device=cur["device"]))
            elif dur and ran < 0.5 * dur:
                out.append(event(user, cur["id"], "skipped", cur["md"], progress=pct, device=cur["device"]))
            del self.current[user]
        for user, nxt in seen.items():
            if user not in self.current:
                self.current[user] = nxt
                out.append(event(user, nxt["id"], "played", nxt["md"], device=nxt["device"],
                                 dedupe=f"nd:{user}:{nxt['id']}:{int(time.time() - (now - nxt['started'])) // 60}"))
        return out


async def catalog(client: httpx.AsyncClient) -> int:
    n, offset = 0, 0
    while True:
        body = await sub(client, "search3", query="", songCount=500, songOffset=offset, artistCount=0, albumCount=0)
        songs = body.get("searchResult3", {}).get("song", [])
        for s in songs:
            ok = await post(client, {"user_id": muse_user(ND_USER), "source": "navidrome", "source_item_id": s["id"], "item_type": "track",
                                     "action": "cataloged", "metadata": {**song_md(s), "dedupe_key": f"nd:cat:{s['id']}:{s.get('title', '')}"}})
            n += ok
        if len(songs) < 500:
            return n
        offset += 500


async def stars(client: httpx.AsyncClient, known: dict[str, set[str]]) -> int:
    n = 0
    for user, pw in ND_USERS:
        body = await sub(client, "getStarred2", user, pw)
        songs = {s["id"]: s for s in body.get("starred2", {}).get("song", [])}
        before = known.get(user)
        for sid, s in songs.items():
            if before is None or sid not in before:
                n += await post(client, event(user, sid, "favorited", song_md(s), dedupe=f"nd:star:{user}:{sid}:{s.get('starred', '')}"))
        for sid in (before or set()) - set(songs):
            n += await post(client, event(user, sid, "unfavorited", {}, dedupe=f"nd:unstar:{user}:{sid}:{int(time.time())}"))
        known[user] = set(songs)
    return n


async def main() -> None:
    np, known, last = NowPlaying(), {}, {"catalog": -1e9, "stars": -1e9}
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            now = time.monotonic()
            try:
                if now - last["catalog"] >= CATALOG_SECONDS:
                    log.info("catalog: %d songs", await catalog(client))
                    last["catalog"] = now
                if ND_USERS and now - last["stars"] >= STARS_SECONDS:
                    await stars(client, known)
                    last["stars"] = now
                body = await sub(client, "getNowPlaying")
                for e in np.update(body.get("nowPlaying", {}).get("entry", []), time.monotonic()):
                    await post(client, e)
            except Exception:
                log.exception("navidrome poll failed")
            await asyncio.sleep(NOW_PLAYING_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
