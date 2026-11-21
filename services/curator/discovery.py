"""Discovery candidates for the curator: titles Muse doesn't have yet.

Two pools, best first:
  1. TMDB recommendations of titles in the library (found by the embedding worker's
     enrichment, already embedded and tagged): "fans of what you love also loved this".
  2. TMDB trending (and Trakt trending, when configured), embedded here with the same
     embedder and document format as the worker, so scores are comparable.

This is the only part of Muse that reaches the public internet for discovery, which is why
it's separate from the inbound-listening adapters (see services/ingestion-api/app/auth.py).
"""

from __future__ import annotations

import logging

import httpx
import numpy as np
from muse_taste.documents import build_document, build_tags, merged_fields
from muse_taste.ranking import CandidateInput
from muse_taste.store import ITEMS_COLLECTION, blind_canon, open_payload
from muse_taste.tmdb import TMDBClient
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logger = logging.getLogger("muse.curator.discovery")

TRAKT_BASE = "https://api.trakt.tv"

# Rough per-item size estimates used for budget math until the arr-stack is queried for
# real file sizes post-download. Deliberately conservative (slightly high) so the curator
# errs toward staying under budget rather than over.
ESTIMATED_BYTES = {
    "movie": 4 * 1024**3,  # 4GB
    "series_episode": 1 * 1024**3,  # 1GB (per series add: Sonarr monitors; size grows as episodes arrive)
}


def catalog_candidates(qdrant: QdrantClient, arr_item_type: str, limit: int = 3000) -> list[CandidateInput]:
    kind = "movie" if arr_item_type == "movie" else "series"
    flt = qm.Filter(must=[qm.FieldCondition(key="item_type", match=qm.MatchValue(value=kind)),
                          qm.FieldCondition(key="available", match=qm.MatchValue(value=False))])
    out, offset = [], None
    while len(out) < limit:
        pts, offset = qdrant.scroll(ITEMS_COLLECTION, scroll_filter=flt, limit=500, offset=offset, with_vectors=True, with_payload=True)
        for p in pts:
            pl = open_payload(p.payload)
            if str(pl.get("real_key") or "").startswith("tmdb:"):
                out.append(_cand(pl["canon_key"], np.asarray(p.vector, dtype=np.float32), pl))
        if offset is None:
            break
    return out


def _cand(key: str, vec: np.ndarray, pl: dict) -> CandidateInput:
    return CandidateInput(key=key, vector=vec, tags=pl.get("tags") or [], vote_average=pl.get("vote_average"), vote_count=pl.get("vote_count"),
                          payload={"title": pl.get("title"), "year": pl.get("year"), "collection_id": pl.get("collection_id"), "real_key": pl.get("real_key")})


async def trending_candidates(tmdb: TMDBClient | None, http: httpx.AsyncClient, trakt_client_id: str | None, embedder, arr_item_type: str) -> list[CandidateInput]:
    kind = "movie" if arr_item_type == "movie" else "tv"
    item_type = "movie" if kind == "movie" else "series"
    raw: dict[int, dict] = {}
    if tmdb is not None:
        try:
            for r in await tmdb.trending(kind):
                raw[r["tmdb_id"]] = r
        except httpx.HTTPError:
            logger.exception("TMDB trending failed")
    if trakt_client_id:
        try:
            resp = await http.get(f"{TRAKT_BASE}/{'movies' if kind == 'movie' else 'shows'}/trending",
                                  headers={"trakt-api-version": "2", "trakt-api-key": trakt_client_id}, params={"limit": 50})
            resp.raise_for_status()
            for r in resp.json():
                item = r.get("movie") or r.get("show") or {}
                tid = (item.get("ids") or {}).get("tmdb")
                if tid and tid not in raw:
                    raw[tid] = {"tmdb_id": tid, "title": item.get("title", ""), "year": item.get("year"), "overview": item.get("overview") or "", "genres": []}
        except httpx.HTTPError:
            logger.exception("Trakt fetch failed")
    if not raw:
        return []
    fields = [merged_fields(item_type, {"title": r["title"], "year": r.get("year"), "genres": r.get("genres") or [], "description": r.get("overview") or ""}) for r in raw.values()]
    vecs = embedder.embed([build_document(f) for f in fields])
    return [
        CandidateInput(key=blind_canon(f"tmdb:{kind}:{r['tmdb_id']}"), vector=v, tags=build_tags(f), vote_average=r.get("vote_average"), vote_count=r.get("vote_count"),
                       payload={"title": r["title"], "year": r.get("year"), "real_key": f"tmdb:{kind}:{r['tmdb_id']}"})
        for r, f, v in zip(raw.values(), fields, vecs)
    ]
