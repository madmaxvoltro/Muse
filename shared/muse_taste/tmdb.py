"""TMDB enrichment: resolve titles to TMDB ids and pull what makes content understanding good.

Per title (one request with append_to_response): keywords, credits (directors, writers, top
cast), production companies / networks, collection, original language, countries, vote
average/count, popularity and TMDB's *recommendations* and *similar* lists. Recommendations
come from what TMDB's users watch together, which gives a single household collaborative
filtering it could never compute from its own data ("borrowed CF", retrieval.related_support).

Privacy: TMDB sees which titles are looked up (roughly: what's in your library and what you
imported), never who watched what or when. Results are cached in Postgres and refreshed
monthly. Auth: TMDB_API_KEY as a v3 key, or a v4 read-access token (starts with "eyJ").
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("muse.tmdb")

BASE = "https://api.themoviedb.org/3"


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None


def parse_details(kind: str, d: dict[str, Any], genre_names: dict[int, str] | None = None) -> dict[str, Any]:
    """TMDB /movie or /tv detail response (with appended keywords, credits/aggregate_credits,
    recommendations, similar) -> Muse enrichment dict."""
    genre_names = genre_names or {}
    kw = d.get("keywords") or {}
    keywords = [k["name"] for k in (kw.get("keywords") or kw.get("results") or []) if k.get("name")]
    credits = d.get("credits") or d.get("aggregate_credits") or {}
    crew = credits.get("crew") or []
    directors = [c["name"] for c in crew if c.get("job") == "Director" or "Director" in [j.get("job") for j in c.get("jobs", [])]]
    writers = [c["name"] for c in crew if c.get("department") == "Writing"][:4]
    if kind == "tv":
        directors = [c["name"] for c in d.get("created_by") or []] + directors
    cast = [c["name"] for c in sorted(credits.get("cast") or [], key=lambda c: c.get("order", 999))[:8]]

    def rel_list(block: dict | None) -> list[dict]:
        out = []
        for r in (block or {}).get("results") or []:
            out.append(
                {
                    "tmdb_id": r.get("id"),
                    "kind": r.get("media_type") or kind,
                    "title": r.get("title") or r.get("name") or "",
                    "overview": r.get("overview") or "",
                    "year": _year(r.get("release_date") or r.get("first_air_date")),
                    "genres": [genre_names[g] for g in r.get("genre_ids") or [] if g in genre_names],
                    "vote_average": r.get("vote_average"),
                    "vote_count": r.get("vote_count"),
                    "popularity": r.get("popularity"),
                    "original_language": r.get("original_language"),
                }
            )
        return out

    coll = d.get("belongs_to_collection")
    return {
        "tmdb_id": d.get("id"),
        "kind": kind,
        "title": d.get("title") or d.get("name"),
        "original_title": d.get("original_title") or d.get("original_name"),
        "year": _year(d.get("release_date") or d.get("first_air_date")),
        "overview": d.get("overview") or "",
        "tagline": d.get("tagline") or None,
        "genres": [g["name"] for g in d.get("genres") or [] if g.get("name")],
        "keywords": keywords,
        "directors": list(dict.fromkeys(directors))[:3],
        "writers": list(dict.fromkeys(writers))[:3],
        "cast": cast,
        "companies": [c["name"] for c in d.get("production_companies") or [] if c.get("name")][:4],
        "networks": [n["name"] for n in d.get("networks") or [] if n.get("name")][:3],
        "original_language": d.get("original_language"),
        "countries": [c.get("iso_3166_1") for c in d.get("production_countries") or [] if c.get("iso_3166_1")]
        or list(d.get("origin_country") or []),
        "collection": {"id": coll.get("id"), "name": coll.get("name")} if coll else None,
        "runtime": d.get("runtime") or ((d.get("episode_run_time") or [None])[0]),
        "vote_average": d.get("vote_average"),
        "vote_count": d.get("vote_count"),
        "popularity": d.get("popularity"),
        "imdb_id": d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id"),
        "tvdb_id": (d.get("external_ids") or {}).get("tvdb_id"),  # Sonarr adds series by TVDB id
        "recommendations": rel_list(d.get("recommendations")),
        "similar": rel_list(d.get("similar"))[:10],
        "fetched_at": int(time.time()),
    }


class TMDBClient:
    """Small async client with a request-rate cap (TMDB allows ~40 req/s; we stay far below)."""

    def __init__(self, credential: str, client: Any, rate_per_sec: float = 8.0, language: str = "en-US"):
        self._client = client
        self._lang = language
        if credential.startswith("eyJ"):
            self._headers = {"Authorization": f"Bearer {credential}"}
            self._params: dict[str, str] = {}
        else:
            self._headers = {}
            self._params = {"api_key": credential}
        self._min_interval = 1.0 / rate_per_sec
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._genres: dict[str, dict[int, str]] = {}

    async def _get(self, path: str, **params: Any) -> dict[str, Any] | None:
        async with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        for attempt in range(3):
            resp = await self._client.get(f"{BASE}{path}", params={**self._params, "language": self._lang, **params}, headers=self._headers)
            if resp.status_code == 429:
                await asyncio.sleep(float(resp.headers.get("Retry-After", "2")) + attempt)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        return None

    async def genre_names(self, kind: str) -> dict[int, str]:
        if kind not in self._genres:
            d = await self._get(f"/genre/{kind}/list") or {}
            self._genres[kind] = {g["id"]: g["name"] for g in d.get("genres", [])}
        return self._genres[kind]

    async def find(self, external_id: str, source: str) -> tuple[str, int] | None:
        """source: imdb_id | tvdb_id. Returns (kind, tmdb_id)."""
        d = await self._get(f"/find/{external_id}", external_source=source)
        if not d:
            return None
        for key, kind in (("movie_results", "movie"), ("tv_results", "tv")):
            if d.get(key):
                return kind, int(d[key][0]["id"])
        if d.get("tv_episode_results"):
            return "tv", int(d["tv_episode_results"][0]["show_id"])
        return None

    async def search(self, kind: str, title: str, year: int | None = None) -> int | None:
        params: dict[str, Any] = {"query": title, "include_adult": "false"}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        d = await self._get(f"/search/{kind}", **params)
        results = (d or {}).get("results") or []
        if not results and year:
            d = await self._get(f"/search/{kind}", query=title, include_adult="false")
            results = (d or {}).get("results") or []
            # without a year match, only accept a close-year hit
            results = [r for r in results if abs((_year(r.get("release_date") or r.get("first_air_date")) or 0) - year) <= 1]
        return int(results[0]["id"]) if results else None

    async def details(self, kind: str, tmdb_id: int) -> dict[str, Any] | None:
        append = "keywords,credits,recommendations,similar,external_ids" if kind == "movie" else "keywords,aggregate_credits,recommendations,similar,external_ids"
        d = await self._get(f"/{kind}/{tmdb_id}", append_to_response=append)
        if not d:
            return None
        return parse_details(kind, d, await self.genre_names(kind))

    async def trending(self, kind: str, window: str = "week", pages: int = 2) -> list[dict]:
        out: list[dict] = []
        names = await self.genre_names(kind)
        for page in range(1, pages + 1):
            d = await self._get(f"/trending/{kind}/{window}", page=page) or {}
            out += parse_details(kind, {"recommendations": d}, names)["recommendations"]
        return out
