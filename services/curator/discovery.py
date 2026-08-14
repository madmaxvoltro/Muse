"""External catalog sources — TMDB (primary) + Trakt (trending signal), combined per
docs/architecture.md. This is the only part of Muse that calls out to the public
internet, which is why it's architecturally separate from the inbound-listening
adapters (see the module docstring in services/ingestion-api/app/auth.py for the
inbound side of that same principle).
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("muse.curator.discovery")

TMDB_BASE = "https://api.themoviedb.org/3"
TRAKT_BASE = "https://api.trakt.tv"

# Rough per-item size estimates used for budget math until the arr-stack is queried for
# real file sizes post-download. Deliberately conservative (slightly high) so the curator
# errs toward staying under budget rather than over.
ESTIMATED_BYTES = {
    "movie": 4 * 1024**3,       # 4GB
    "series_episode": 1 * 1024**3,  # 1GB
}


@dataclass
class ExternalCandidate:
    source_item_id: str  # tmdb id
    item_type: str
    title: str
    description: str
    genres: list[str]


async def fetch_tmdb_trending(client: httpx.AsyncClient, api_key: str, item_type: str, limit: int = 50) -> list[ExternalCandidate]:
    media_type = "movie" if item_type == "movie" else "tv"
    resp = await client.get(
        f"{TMDB_BASE}/trending/{media_type}/week",
        params={"api_key": api_key},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])[:limit]
    return [
        ExternalCandidate(
            source_item_id=str(r["id"]),
            item_type=item_type,
            title=r.get("title") or r.get("name", ""),
            description=r.get("overview", ""),
            genres=[],  # TMDB trending doesn't include genre names inline; a /movie/{id} lookup would be needed for full genres
        )
        for r in results
    ]


async def fetch_trakt_trending(client: httpx.AsyncClient, client_id: str, item_type: str, limit: int = 50) -> list[ExternalCandidate]:
    media_type = "movies" if item_type == "movie" else "shows"
    resp = await client.get(
        f"{TRAKT_BASE}/{media_type}/trending",
        headers={"trakt-api-version": "2", "trakt-api-key": client_id},
        params={"limit": limit},
    )
    resp.raise_for_status()
    out = []
    for r in resp.json():
        item = r.get("movie") or r.get("show") or {}
        tmdb_id = (item.get("ids") or {}).get("tmdb")
        if tmdb_id is None:
            continue
        out.append(
            ExternalCandidate(
                source_item_id=str(tmdb_id),
                item_type=item_type,
                title=item.get("title", ""),
                description=item.get("overview", "") or "",
                genres=[],
            )
        )
    return out


async def fetch_candidates(
    client: httpx.AsyncClient, tmdb_api_key: str | None, trakt_client_id: str | None, item_type: str
) -> list[ExternalCandidate]:
    """Combines TMDB + Trakt, deduped by tmdb id. Either key can be omitted (that source
    is skipped) — TMDB alone is enough to function, Trakt only adds trending signal.
    """
    candidates: dict[str, ExternalCandidate] = {}

    if tmdb_api_key:
        try:
            for c in await fetch_tmdb_trending(client, tmdb_api_key, item_type):
                candidates[c.source_item_id] = c
        except httpx.HTTPError:
            logger.exception("TMDB fetch failed")

    if trakt_client_id:
        try:
            for c in await fetch_trakt_trending(client, trakt_client_id, item_type):
                candidates.setdefault(c.source_item_id, c)
        except httpx.HTTPError:
            logger.exception("Trakt fetch failed")

    return list(candidates.values())
