"""Search across everything Muse has seen: semantic (embedding) + keyword, optionally
personalised with a person's taste vector, with their own history attached to each hit."""

from __future__ import annotations

import math
import uuid
from typing import Any

from muse_schema import content_point_id, identity_group, taste_point_id
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


async def _keyword(conn, q: str, item_types, sources, limit: int) -> dict[tuple[str, str], float]:
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = await conn.fetch(
        """
        SELECT source, source_item_id,
               max(CASE WHEN metadata->>'title' ILIKE $2 THEN 1.0
                        WHEN metadata->>'title' ILIKE $1 THEN 0.85
                        WHEN metadata->>'artist' ILIKE $1 OR metadata->>'album' ILIKE $1 THEN 0.7
                        WHEN metadata::text ILIKE $1 THEN 0.45 ELSE 0 END) AS s
        FROM event
        WHERE source_item_id IS NOT NULL AND metadata <> '{}'::jsonb AND metadata::text ILIKE $1
          AND ($3::text[] IS NULL OR item_type = ANY($3)) AND ($4::text[] IS NULL OR source = ANY($4))
        GROUP BY 1, 2 ORDER BY s DESC LIMIT $5
        """,
        like, q.replace("%", "") + "%", item_types, sources, limit,
    )
    return {(r["source"], r["source_item_id"]): float(r["s"]) for r in rows if r["s"]}


async def search(
    conn, qdrant, embedder, q: str, *, item_types: list[str] | None = None, sources: list[str] | None = None,
    limit: int = 20, user_id: uuid.UUID | None = None, personalize: bool = True, include_seen: bool = True,
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
        for k, s in (await _keyword(conn, q, item_types, sources, limit * 3)).items():
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
