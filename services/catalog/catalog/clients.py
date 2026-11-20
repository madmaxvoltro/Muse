"""Clients for the public music databases the catalog draws on.

* **MusicBrainz** — the canonical ids (artists, release groups, recordings), genres/tags and
  every release an artist ever put out. 1 request/second, a descriptive User-Agent required.
* **ListenBrainz** — listening-based similarity between artists and an artist's most played
  recordings (open data from millions of listens).
* **Deezer** — no key needed: related artists, top tracks with 30-second previews (which the
  audio analyzer listens to), BPM and ISRC.

Everything here is best-effort: a source that's down or changed shape yields nothing, never
an exception that stops the cycle. Nothing personal is sent — only artist names and ids.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("muse.catalog.clients")

USER_AGENT = os.environ.get(
    "MUSE_CATALOG_USER_AGENT", "MuseCatalog/0.1 ( self-hosted recommender; https://github.com/madmaxvoltro/Muse )"
)
MB_URL = os.environ.get("MUSE_MUSICBRAINZ_URL", "https://musicbrainz.org/ws/2").rstrip("/")
LB_URL = os.environ.get("MUSE_LISTENBRAINZ_URL", "https://api.listenbrainz.org/1").rstrip("/")
LB_LABS_URL = os.environ.get("MUSE_LISTENBRAINZ_LABS_URL", "https://labs.api.listenbrainz.org").rstrip("/")
LB_SIMILAR_ALGORITHM = os.environ.get(
    "MUSE_LISTENBRAINZ_SIMILAR_ALGORITHM",
    "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30",
)
DEEZER_URL = os.environ.get("MUSE_DEEZER_URL", "https://api.deezer.com").rstrip("/")


class _Throttle:
    def __init__(self, per_second: float) -> None:
        self.gap = 1.0 / per_second
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if now < self.next:
                await asyncio.sleep(self.next - now)
            self.next = max(now, self.next) + self.gap


class Http:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client = httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                        transport=transport, follow_redirects=True)

    async def get(self, url: str, params: dict | None = None, throttle: _Throttle | None = None) -> Any:
        for attempt in range(3):
            if throttle:
                await throttle.wait()
            try:
                r = await self.client.get(url, params=params)
            except httpx.HTTPError as e:
                log.info("GET %s failed: %s", url, e)
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 503):
                await asyncio.sleep(float(r.headers.get("Retry-After", 2 ** (attempt + 1))))
                continue
            if r.status_code >= 400:
                log.info("GET %s -> %s", url, r.status_code)
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    async def close(self) -> None:
        await self.client.aclose()


class MusicBrainz:
    def __init__(self, http: Http) -> None:
        self.http, self.throttle = http, _Throttle(1.0)

    async def search_artist(self, name: str) -> list[dict]:
        data = await self.http.get(f"{MB_URL}/artist", {"query": f'artist:"{name}"', "fmt": "json", "limit": 5}, self.throttle)
        return [
            {"mbid": a["id"], "name": a.get("name", ""), "score": int(a.get("score", 0) or 0), "country": a.get("country"),
             "disambiguation": a.get("disambiguation", "")}
            for a in (data or {}).get("artists", []) if a.get("id")
        ]

    async def artist(self, mbid: str) -> dict | None:
        data = await self.http.get(f"{MB_URL}/artist/{mbid}", {"inc": "genres tags", "fmt": "json"}, self.throttle)
        if not data:
            return None
        votes: dict[str, int] = {}
        for t in (data.get("genres") or []) + (data.get("tags") or []):
            if t.get("name"):
                votes[t["name"].lower()] = max(votes.get(t["name"].lower(), 0), int(t.get("count", 0) or 0))
        tags = [n for n, c in sorted(votes.items(), key=lambda x: -x[1]) if c > 0][:12]
        return {"mbid": data.get("id", mbid), "name": data.get("name", ""), "country": data.get("country"), "tags": tags}

    async def release_groups(self, artist_mbid: str) -> list[dict]:
        out, offset = [], 0
        while offset < 300:
            data = await self.http.get(
                f"{MB_URL}/release-group",
                {"artist": artist_mbid, "type": "album|ep|single", "fmt": "json", "limit": 100, "offset": offset},
                self.throttle,
            )
            groups = (data or {}).get("release-groups", [])
            for g in groups:
                out.append({
                    "mbid": g.get("id"), "title": g.get("title", ""), "primary_type": g.get("primary-type"),
                    "secondary_types": g.get("secondary-types") or [], "date": g.get("first-release-date") or None,
                })
            if len(groups) < 100:
                break
            offset += 100
        return [g for g in out if g["mbid"]]


class ListenBrainz:
    def __init__(self, http: Http) -> None:
        self.http, self.throttle = http, _Throttle(2.0)

    async def similar_artists(self, artist_mbid: str, limit: int = 25) -> list[dict]:
        data = await self.http.get(
            f"{LB_LABS_URL}/similar-artists/json",
            {"artist_mbids": artist_mbid, "algorithm": LB_SIMILAR_ALGORITHM},
            self.throttle,
        )
        rows = data if isinstance(data, list) else (data or {}).get("payload", [])
        # The labs API has returned both a flat list and a list of {"data": [...]} blocks.
        if rows and isinstance(rows[0], dict) and "data" in rows[0]:
            rows = [r for block in rows for r in block.get("data", [])]
        out = []
        for r in rows or []:
            mbid = r.get("artist_mbid") or r.get("similar_artist_mbid")
            name = r.get("name") or r.get("similar_artist_name")
            if mbid and name and mbid != artist_mbid:
                out.append({"mbid": mbid, "name": name, "score": float(r.get("score", 0) or 0), "via": "listenbrainz"})
        return sorted(out, key=lambda x: -x["score"])[:limit]

    async def top_recordings(self, artist_mbid: str, limit: int = 10) -> list[dict]:
        data = await self.http.get(f"{LB_URL}/popularity/top-recordings-for-artist/{artist_mbid}", None, self.throttle)
        rows = data if isinstance(data, list) else (data or {}).get("payload", []) or []
        out = []
        for r in rows[:limit]:
            if r.get("recording_mbid") and r.get("recording_name"):
                out.append({
                    "recording_mbid": r["recording_mbid"], "title": r["recording_name"], "artist": r.get("artist_name", ""),
                    "album": r.get("release_name"), "listens": int(r.get("total_listen_count", 0) or 0),
                    "release_mbid": r.get("release_mbid"),
                })
        return out


class Deezer:
    def __init__(self, http: Http) -> None:
        self.http, self.throttle = http, _Throttle(8.0)  # Deezer allows 50 requests / 5 s

    async def search_artist(self, name: str) -> dict | None:
        data = await self.http.get(f"{DEEZER_URL}/search/artist", {"q": name, "limit": 5}, self.throttle)
        rows = (data or {}).get("data", [])
        return {"id": rows[0]["id"], "name": rows[0].get("name", ""), "fans": rows[0].get("nb_fan", 0)} if rows else None

    async def related(self, deezer_id: int, limit: int = 20) -> list[dict]:
        data = await self.http.get(f"{DEEZER_URL}/artist/{deezer_id}/related", {"limit": limit}, self.throttle)
        rows = (data or {}).get("data", [])
        top = max([r.get("nb_fan", 0) or 0 for r in rows] or [1]) or 1
        return [{"deezer_id": r["id"], "name": r.get("name", ""), "score": 0.5 + 0.5 * (r.get("nb_fan", 0) or 0) / top, "via": "deezer"}
                for r in rows if r.get("id")]

    async def top_tracks(self, deezer_id: int, limit: int = 10) -> list[dict]:
        data = await self.http.get(f"{DEEZER_URL}/artist/{deezer_id}/top", {"limit": limit}, self.throttle)
        out = []
        for r in (data or {}).get("data", []):
            if not r.get("id"):
                continue
            out.append({
                "deezer_id": r["id"], "title": r.get("title", ""), "artist": (r.get("artist") or {}).get("name", ""),
                "album": (r.get("album") or {}).get("title"), "duration_s": r.get("duration"), "rank": r.get("rank", 0),
                "preview_url": r.get("preview") or None, "explicit": bool(r.get("explicit_lyrics")),
            })
        return out

    async def track(self, deezer_id: int) -> dict | None:
        data = await self.http.get(f"{DEEZER_URL}/track/{deezer_id}", None, self.throttle)
        if not data or "error" in data:
            return None
        return {"bpm": data.get("bpm") or None, "gain": data.get("gain"), "isrc": data.get("isrc"),
                "release_date": data.get("release_date"), "preview_url": data.get("preview") or None}
