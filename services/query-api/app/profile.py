"""A readable summary of a person's taste — what an assistant reads before it talks about it."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from muse_schema import identity_group, taste_point_id
from muse_schema.storage import fetch_open

from .identity import describe_identity
from .items import DOMAIN_LABELS, ITEM_TYPES, item_details, summarize_metadata
from .knowledge import get_overlay, list_facts

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
    # Genres, artists, favourites and titles live in the sealed metadata (muse_schema.storage),
    # so they're aggregated here in Python over the opened events of the window.
    window = await fetch_open(
        conn,
        """
        SELECT item_type, source, source_item_id, action, action_weight, "timestamp", metadata FROM event
        WHERE user_id = ANY($1::uuid[]) AND action <> 'cataloged' AND item_type = ANY($2::text[] || '{search_query}')
          AND "timestamp" > now() - make_interval(days => $3::int)
        ORDER BY "timestamp" DESC
        """,
        group, types, days,
    )
    titles: dict[tuple[str, str], str] = {}
    for r in window:
        k = (r["source"], r["source_item_id"])
        if r["source_item_id"] and k not in titles and (r["metadata"] or {}).get("title"):
            titles[k] = r["metadata"]["title"]
    missing = list({(r["source"], r["source_item_id"]) for r in window if r["source_item_id"]} - set(titles))
    if missing:
        for k, d in (await item_details(conn, missing)).items():
            if d["metadata"].get("title"):
                titles[k] = d["metadata"]["title"]
    facet_acc: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0])
    fav_acc: dict[tuple[str, str, str], list] = {}
    fav_actions = {"favorited", "rated", "completed", "added_to_playlist", "unfavorited", "rejected", "skipped"}
    for r in window:
        if r["item_type"] not in types:
            continue
        w = float(r["action_weight"] or 0)
        md = r["metadata"] or {}
        if r["action"] != "searched" and w != 0:
            for g in (md.get("genres") if isinstance(md.get("genres"), list) else []) or []:
                a = facet_acc[(r["item_type"], "genre", g)]
                a[0] += w
                a[1] += 1
            if md.get("artist"):
                a = facet_acc[(r["item_type"], "artist", md["artist"])]
                a[0] += w
                a[1] += 1
        if r["source_item_id"] and r["action"] in fav_actions:
            f = fav_acc.setdefault((r["item_type"], r["source"], r["source_item_id"]), [0.0, r["timestamp"]])
            f[0] += w
    facets = [{"item_type": t, "facet": fa, "value": v, "w": a[0], "n": a[1]} for (t, fa, v), a in facet_acc.items()]
    favourites = [{"item_type": t, "source": s_, "source_item_id": sid, "w": f[0], "last": f[1], "title": titles.get((s_, sid))}
                  for (t, s_, sid), f in fav_acc.items()]
    recent = [{**r, "query": (r["metadata"] or {}).get("query"), "title": titles.get((r["source"], r["source_item_id"]))}
              for r in window[:15]]

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
    rows = await fetch_open(
        conn,
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
