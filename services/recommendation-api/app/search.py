"""Search by meaning: the query is embedded with the content model and matched against
every title's vector. "slow space movie about grief" finds Solaris without the words being
in its title. With a user, results lean slightly toward their taste."""

import asyncio

from qdrant_client import QdrantClient
from qdrant_client.http.models import FieldCondition, Filter, MatchAny

from . import ratings
from .taste import affinity, cos

MIN_SIMILARITY = 0.22
TASTE_WEIGHT = 0.12


async def semantic(qdrant: QdrantClient, query_vec, item_types: list[str], limit: int, profile=None,
                   max_age: int | None = None, allow_unrated: bool = False) -> list[dict]:
    types = list(dict.fromkeys(item_types + (["series_episode"] if "series" in item_types else [])))
    pts = await asyncio.to_thread(
        lambda: qdrant.query_points(
            collection_name="content_items", query=list(query_vec),
            query_filter=Filter(must=[FieldCondition(key="item_type", match=MatchAny(any=types))]),
            limit=limit * 4, with_payload=True, with_vectors=profile is not None,
        ).points
    )
    best: dict[str, dict] = {}
    for h in pts:
        pl = h.payload or {}
        if h.score < MIN_SIMILARITY or not ratings.within(pl.get("official_rating"), max_age, allow_unrated):
            continue
        sid, typ, title = pl.get("source_item_id"), pl.get("item_type"), pl.get("title", "")
        if typ == "series_episode" and "series" in item_types and pl.get("series_id"):
            sid, typ, title = pl["series_id"], "series", pl.get("series_name") or title
        elif typ == "series_episode" and "series_episode" not in item_types:
            continue
        score = float(h.score)
        if profile is not None and not profile.cold and h.vector is not None:
            score += TASTE_WEIGHT * affinity(h.vector, profile.facets, profile.taste)
        key = f"{pl.get('source')}:{sid}"
        if key not in best or score > best[key]["score"]:
            best[key] = {"source": pl.get("source"), "source_item_id": sid, "item_type": typ, "title": title,
                         "score": round(score, 4), "similarity": round(float(h.score), 4)}
    return sorted(best.values(), key=lambda r: -r["score"])[:limit]


__all__ = ["semantic", "cos"]
