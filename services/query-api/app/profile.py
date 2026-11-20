"""A readable summary of a person's taste — what an assistant reads before it talks about it."""

from __future__ import annotations

import uuid
from typing import Any

from muse_schema import identity_group, taste_point_id

from .identity import describe_identity
from .items import DOMAIN_LABELS, ITEM_TYPES, summarize_metadata
from .knowledge import get_overlay, list_facts

TITLE_SQL = """(SELECT e2.metadata->>'title' FROM event e2 WHERE e2.source = e.source
               AND e2.source_item_id = e.source_item_id AND e2.metadata ? 'title' ORDER BY e2."timestamp" DESC LIMIT 1)"""


async def build_profile(conn, qdrant, user_id: uuid.UUID, domain: str | None = None, days: int = 365) -> dict[str, Any]:
    _, group = await identity_group(conn, user_id)
    types = [domain] if domain else ITEM_TYPES

    counts = await conn.fetch(
        """
        SELECT item_type, count(*) AS n,
               count(*) FILTER (WHERE action_weight > 0) AS positive,
               count(*) FILTER (WHERE action_weight < 0) AS negative,
               max("timestamp") AS last
        FROM event WHERE user_id = ANY($1::uuid[]) AND action NOT IN ('cataloged') AND item_type = ANY($2::text[])
          AND "timestamp" > now() - make_interval(days => $3::int)
        GROUP BY 1
        """,
        group, types, days,
    )
    facets = await conn.fetch(
        """
        WITH ev AS (
            SELECT item_type, action_weight AS w, metadata FROM event
            WHERE user_id = ANY($1::uuid[]) AND action NOT IN ('cataloged', 'searched') AND action_weight <> 0
              AND item_type = ANY($2::text[]) AND "timestamp" > now() - make_interval(days => $3::int)
        )
        SELECT item_type, 'genre' AS facet, g AS value, sum(w) AS w, count(*) AS n FROM ev,
            jsonb_array_elements_text(CASE WHEN jsonb_typeof(metadata->'genres') = 'array' THEN metadata->'genres' ELSE '[]'::jsonb END) g
        GROUP BY 1, 2, 3
        UNION ALL
        SELECT item_type, 'artist', metadata->>'artist', sum(w), count(*) FROM ev
        WHERE metadata ? 'artist' GROUP BY 1, 2, 3
        """,
        group, types, days,
    )
    favourites = await conn.fetch(
        f"""
        SELECT e.item_type, e.source, e.source_item_id, sum(e.action_weight) AS w, max(e."timestamp") AS last, {TITLE_SQL} AS title
        FROM event e
        WHERE e.user_id = ANY($1::uuid[]) AND e.source_item_id IS NOT NULL AND e.item_type = ANY($2::text[])
          AND e.action IN ('favorited', 'rated', 'completed', 'added_to_playlist', 'unfavorited', 'rejected', 'skipped')
          AND e."timestamp" > now() - make_interval(days => $3::int)
        GROUP BY 1, 2, 3
        """,
        group, types, days,
    )
    recent = await conn.fetch(
        f"""
        SELECT e.item_type, e.source, e.source_item_id, e.action, e."timestamp", e.metadata->>'query' AS query, {TITLE_SQL} AS title
        FROM event e WHERE e.user_id = ANY($1::uuid[]) AND e.action <> 'cataloged' AND e.item_type = ANY($2::text[] || '{{search_query}}')
        ORDER BY e."timestamp" DESC LIMIT 15
        """,
        group, types,
    )

    domains: dict[str, Any] = {}
    for c in counts:
        domains[c["item_type"]] = {
            "label": DOMAIN_LABELS.get(c["item_type"], c["item_type"]),
            "events": c["n"], "positive": c["positive"], "negative": c["negative"],
            "last_active": c["last"].isoformat(),
            "loves": [], "dislikes": [], "top_artists": [], "favourites": [], "least_favourite": [],
        }
    for f in facets:
        d = domains.get(f["item_type"])
        if d is None:
            continue
        entry = {"name": f["value"], "weight": round(float(f["w"]), 2), "events": f["n"]}
        if f["facet"] == "genre":
            (d["loves"] if f["w"] > 0 else d["dislikes"]).append(entry)
        elif f["w"] > 0:
            d["top_artists"].append(entry)
    for f in favourites:
        d = domains.get(f["item_type"])
        if d is None or not f["title"]:
            continue
        entry = {"title": f["title"], "source": f["source"], "source_item_id": f["source_item_id"], "weight": round(float(f["w"]), 2)}
        (d["favourites"] if f["w"] > 0 else d["least_favourite"]).append(entry)
    for d in domains.values():
        d["loves"] = sorted(d["loves"], key=lambda x: -x["weight"])[:10]
        d["dislikes"] = sorted(d["dislikes"], key=lambda x: x["weight"])[:6]
        d["top_artists"] = sorted(d["top_artists"], key=lambda x: -x["weight"])[:10]
        d["favourites"] = sorted(d["favourites"], key=lambda x: -x["weight"])[:10]
        d["least_favourite"] = sorted(d["least_favourite"], key=lambda x: x["weight"])[:5]
        if not d["top_artists"]:
            del d["top_artists"]

    buckets = [*types, "_combined"]
    pts = qdrant.retrieve(collection_name="taste_vectors", ids=[taste_point_id(str(user_id), b) for b in buckets], with_payload=True) \
        if _has_collection(qdrant, "taste_vectors") else []
    taste_vectors = {
        (p.payload or {}).get("item_type"): {k: v for k, v in (p.payload or {}).items() if k in ("events", "facts")} for p in pts
    }

    open_q = await conn.fetchval("SELECT count(*) FROM question WHERE user_id = $1 AND status = 'open'", user_id)
    return {
        "user_id": str(user_id),
        "window_days": days,
        "domains": domains,
        "facts": await list_facts(conn, user_id, domain),
        "settings": await get_overlay(conn, user_id),
        "taste_vectors": taste_vectors,
        "identity": await describe_identity(conn, user_id),
        "open_questions": open_q,
        "recent": [
            {"item_type": r["item_type"], "action": r["action"], "at": r["timestamp"].isoformat(),
             "title": r["title"] or r["query"], "source": r["source"], "source_item_id": r["source_item_id"]}
            for r in recent
        ],
    }


def _has_collection(qdrant, name: str) -> bool:
    try:
        return qdrant.collection_exists(name)
    except Exception:
        return False


async def history(conn, user_id: uuid.UUID, item_type: str | None, action: str | None, limit: int, before: str | None) -> list[dict]:
    _, group = await identity_group(conn, user_id)
    rows = await conn.fetch(
        """
        SELECT source, source_item_id, item_type, action, action_weight, "timestamp", progress_pct, metadata
        FROM event WHERE user_id = ANY($1::uuid[]) AND action <> 'cataloged'
          AND ($2::text IS NULL OR item_type = $2) AND ($3::text IS NULL OR action = $3)
          AND ($4::timestamptz IS NULL OR "timestamp" < $4)
        ORDER BY "timestamp" DESC LIMIT $5
        """,
        group, item_type, action, _ts(before), limit,
    )
    return [
        {"source": r["source"], "source_item_id": r["source_item_id"], "item_type": r["item_type"], "action": r["action"],
         "weight": round(float(r["action_weight"]), 3), "at": r["timestamp"].isoformat(), "progress_pct": r["progress_pct"],
         **summarize_metadata(r["item_type"], r["metadata"] or {}),
         **({"query": r["metadata"].get("query")} if (r["metadata"] or {}).get("query") else {})}
        for r in rows
    ]


def _ts(v: str | None):
    if not v:
        return None
    from datetime import datetime

    return datetime.fromisoformat(v.replace("Z", "+00:00"))
