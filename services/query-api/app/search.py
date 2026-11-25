"""Search across everything Muse has seen: semantic (embedding) + keyword, optionally
personalised with a person's taste vector, with their own history attached to each hit."""

from __future__ import annotations

import math
import uuid
from typing import Any

from muse_schema import content_point_id, identity_group, ratings, taste_point_id
from qdrant_client.http.models import FieldCondition, Filter, MatchAny

from .items import interactions, item_details, summarize_metadata

W_MATCH, W_TASTE = 0.7, 0.3


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _filter(item_types: list[str] | None, sources: list[str] | None) -> Filter | None:
    must = []
    if item_types:
        must.append(FieldCondition(key="item_type", match=MatchAny(any=item_types)))
    if sources:
        must.append(FieldCondition(key="source", match=MatchAny(any=sources)))
    return Filter(must=must) if must else None


# Keyword search can't run in SQL: titles live in sealed event metadata (muse_schema.storage).
# It runs on a small in-memory index of every item's title/artist/album, read from the
# content_items collection (payloads opened by muse_schema.vectors) and refreshed every few
# minutes. One entry per item, so this stays small even for large libraries.
_INDEX: dict = {"at": 0.0, "items": [], "n": -1}
INDEX_TTL_S = 300


def _keyword_index(qdrant) -> list[tuple[str, str, str, str, str, str]]:
    import time

    try:  # rebuilt when items were added or removed, else at most every INDEX_TTL_S
        n = qdrant.count("content_items", exact=False).count
    except Exception:
        n = -1
    if _INDEX["items"] and n == _INDEX["n"] and time.monotonic() - _INDEX["at"] < INDEX_TTL_S:
        return _INDEX["items"]
    items, offset = [], None
    try:
        while True:
            pts, offset = qdrant.scroll("content_items", limit=1000, offset=offset, with_payload=True, with_vectors=False)
            for pt in pts:
                pl = pt.payload or {}
                if pl.get("source_item_id"):
                    items.append((pl.get("source", ""), str(pl["source_item_id"]), pl.get("item_type", ""),
                                  (pl.get("title") or "").casefold(), (pl.get("artist") or "").casefold(), (pl.get("album") or "").casefold()))
            if offset is None:
                break
    except Exception:  # no collection yet
        return _INDEX["items"]
    _INDEX.update(at=time.monotonic(), items=items, n=n)
    return items


async def _keyword(conn, qdrant, q: str, item_types, sources, limit: int) -> dict[tuple[str, str], float]:
    needle = q.strip().casefold()
    if not needle:
        return {}
    out: dict[tuple[str, str], float] = {}
    for source, sid, item_type, title, artist, album in _keyword_index(qdrant):
        if (item_types and item_type not in item_types) or (sources and source not in sources):
            continue
        if title.startswith(needle):
            s_ = 1.0
        elif needle in title:
            s_ = 0.85
        elif needle in artist or needle in album:
            s_ = 0.7
        else:
            continue
        out[(source, sid)] = s_
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:limit])


async def search(
    conn, qdrant, embedder, q: str, *, item_types: list[str] | None = None, sources: list[str] | None = None,
    limit: int = 20, user_id: uuid.UUID | None = None, personalize: bool = True, include_seen: bool = True,
    max_age: int | None = None, allow_unrated: bool = False,
) -> dict[str, Any]:
    q = q.strip()
    scores: dict[tuple[str, str], dict[str, float]] = {}
    mode = []
    if embedder is not None and q:
        vec = embedder.encode([q])[0]
        hits = qdrant.query_points(collection_name="content_items", query=vec, query_filter=_filter(item_types, sources),
                                   limit=limit * 3).points
        for h in hits:
            p = h.payload or {}
            scores.setdefault((p.get("source", ""), p.get("source_item_id", "")), {})["semantic"] = float(h.score)
        mode.append("semantic")
    if q:
        for k, s in (await _keyword(conn, qdrant, q, item_types, sources, limit * 3)).items():
            scores.setdefault(k, {})["keyword"] = s
        mode.append("keyword")

    keys = list(scores)
    taste: dict[str, list[float]] = {}
    item_vecs: dict[str, list[float]] = {}
    group: list[uuid.UUID] = []
    if user_id is not None:
        _, group = await identity_group(conn, user_id)
    if user_id is not None and personalize and keys:
        buckets = [*(item_types or []), "_combined"]
        tps = qdrant.retrieve(collection_name="taste_vectors", ids=[taste_point_id(str(user_id), b) for b in buckets], with_vectors=True)
        taste = {(p.payload or {}).get("item_type"): list(p.vector) for p in tps}
        pts = qdrant.retrieve(collection_name="content_items", ids=[content_point_id(s, i) for s, i in keys], with_vectors=True, with_payload=True)
        item_vecs = {f"{(p.payload or {}).get('source')}:{(p.payload or {}).get('source_item_id')}": list(p.vector) for p in pts}
        if taste:
            mode.append("personalized")

    details = await item_details(conn, keys)
    seen = await interactions(conn, group, keys) if group else {}
    results = []
    for k, s in scores.items():
        d = details.get(k)
        if d is None:
            continue
        match = max(s.get("semantic", 0.0), s.get("keyword", 0.0)) + (0.05 if len(s) > 1 else 0.0)
        score, fit = match, None
        t = taste.get(d["item_type"]) or taste.get("_combined")
        v = item_vecs.get(f"{k[0]}:{k[1]}")
        if t is not None and v is not None:
            fit = _cos(t, v)
            score = W_MATCH * match + W_TASTE * fit
        mine = seen.get(k)
        if mine and not include_seen:
            continue
        if max_age is not None and not ratings.within((d["metadata"] or {}).get("official_rating"), max_age, allow_unrated):
            continue  # kids profiles: only titles rated for their age
        results.append({
            "source": k[0], "source_item_id": k[1], "item_type": d["item_type"],
            **summarize_metadata(d["item_type"], d["metadata"]),
            "score": round(score, 4), "match": round(match, 4),
            **({"taste_fit": round(fit, 4)} if fit is not None else {}),
            **({"you": mine} if mine else {}),
        })
    results.sort(key=lambda r: -r["score"])
    return {"query": q, "mode": mode, "results": results[:limit]}


async def similar(conn, qdrant, source: str, source_item_id: str, *, item_types: list[str] | None = None,
                  limit: int = 10, user_id: uuid.UUID | None = None) -> dict[str, Any]:
    pts = qdrant.retrieve(collection_name="content_items", ids=[content_point_id(source, source_item_id)], with_vectors=True)
    if not pts:
        return {"results": [], "note": "item not embedded yet (unknown, or the embedding worker hasn't seen it)"}
    hits = qdrant.query_points(collection_name="content_items", query=list(pts[0].vector),
                               query_filter=_filter(item_types, None), limit=limit + 1).points
    keys = [((h.payload or {}).get("source", ""), (h.payload or {}).get("source_item_id", "")) for h in hits]
    keys_scores = [(k, h.score) for k, h in zip(keys, hits) if k != (source, source_item_id)][:limit]
    details = await item_details(conn, [k for k, _ in keys_scores])
    seen = {}
    if user_id is not None:
        _, group = await identity_group(conn, user_id)
        seen = await interactions(conn, group, [k for k, _ in keys_scores])
    out = []
    for k, s in keys_scores:
        d = details.get(k) or {"item_type": None, "metadata": {}}
        out.append({"source": k[0], "source_item_id": k[1], "item_type": d["item_type"], "similarity": round(float(s), 4),
                    **summarize_metadata(d["item_type"] or "", d["metadata"]), **({"you": seen[k]} if k in seen else {})})
    return {"results": out}
