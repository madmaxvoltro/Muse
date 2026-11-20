"""Recommendation API — implements the 3-stage funnel from docs/architecture.md:
candidate generation (candidates.py) -> ranking (scoring.score_candidate) ->
re-ranking (scoring.rerank). Explainability comes for free: `reasons` on every
scored candidate is exactly which weighted factors produced its score.
"""

import json
import os
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from muse_schema import identity_group
from qdrant_client import QdrantClient

from .candidates import generate_candidates
from .db import close_pool, get_pool
from .scoring import EXPLORE_RATIO_DEFAULT, apply_knowledge, describe, rerank, score_candidate
from . import soon_gone as soon_gone_repo

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
ITEM_TYPES = ["movie", "series", "series_episode", "youtube_video", "track", "audiobook", "ebook"]
TASTE_BUCKETS = ITEM_TYPES + ["_combined"]

# MUSE_READ_KEYS="vesper:xxxx,jellyfin-plugin:yyyy": callers must send X-Muse-Read-Key. Read
# keys can only read; ingestion keys (MUSE_ADAPTER_KEYS) can only write. Unset = open (dev).
_READ_KEYS = {k.split(":", 1)[1] for k in os.environ.get("MUSE_READ_KEYS", "").split(",") if ":" in k}


async def require_read_key(request: Request, x_muse_read_key: str | None = Header(default=None)) -> None:
    if request.url.path == "/healthz":
        return
    if _READ_KEYS and x_muse_read_key not in _READ_KEYS:
        raise HTTPException(status_code=401, detail="invalid or missing X-Muse-Read-Key")


app = FastAPI(title="Muse Recommendation API", version="0.1.0", dependencies=[Depends(require_read_key)])
_qdrant: QdrantClient | None = None


def qdrant() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def load_knowledge(pool, canonical: uuid.UUID) -> tuple[list[dict], dict]:
    """Active facts and the overlay row (sliders, preferences) for one person.
    Tolerates databases without migration 007 (no facts, plain overlay)."""
    async with pool.acquire() as conn:
        try:
            facts = [
                dict(r)
                for r in await conn.fetch(
                    "SELECT kind, domain, subject, sentiment, confidence FROM profile_fact "
                    "WHERE user_id = $1 AND retracted_at IS NULL",
                    canonical,
                )
            ]
        except Exception:  # undefined_table on an un-migrated database
            facts = []
        try:
            row = await conn.fetchrow(
                "SELECT source_weights, explore_ratio, disabled_sources, preferences FROM user_overlay WHERE user_id = $1",
                canonical,
            )
        except Exception:
            row = await conn.fetchrow(
                "SELECT source_weights, explore_ratio, disabled_sources FROM user_overlay WHERE user_id = $1", canonical
            )
    overlay: dict = {}
    if row:
        for k, v in dict(row).items():
            overlay[k] = json.loads(v) if isinstance(v, str) and k != "explore_ratio" else v
    return facts, overlay


@app.get("/recommendations/{user_id}")
async def recommendations(
    user_id: str,
    item_type: str = Query(..., description=f"one of {ITEM_TYPES}"),
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0.0, le=1.0, description="overrides the explore-slot ratio (default: the person's overlay, else 0.125)"),
    source: list[str] | None = Query(None, description="only items from these sources, e.g. source=mono"),
    taste_from: str | None = Query(None, description=f"rank with another domain's taste, one of {TASTE_BUCKETS} (e.g. music taste -> films)"),
    blend: float | None = Query(None, ge=0.0, le=1.0, description="share of cross-domain taste mixed in (default: overlay preference taste_blend, else 0)"),
) -> dict:
    if item_type not in ITEM_TYPES:
        raise HTTPException(status_code=400, detail=f"item_type must be one of {ITEM_TYPES}")
    if taste_from is not None and taste_from not in TASTE_BUCKETS:
        raise HTTPException(status_code=400, detail=f"taste_from must be one of {TASTE_BUCKETS}")
    try:
        uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id must be a UUID")

    pool = await get_pool()
    async with pool.acquire() as conn:
        canonical, group = await identity_group(conn, user_id)
    facts, overlay = await load_knowledge(pool, canonical)
    prefs = overlay.get("preferences") or {}
    if blend is None:
        blend = float(prefs.get("taste_blend", 0.0) or 0.0)
    if explore_ratio is None:
        explore_ratio = float(overlay.get("explore_ratio") or EXPLORE_RATIO_DEFAULT)

    candidates = await generate_candidates(
        pool, qdrant(), str(canonical), item_type,
        group=group, taste_from=taste_from, blend=blend,
        sources=source, exclude_sources=overlay.get("disabled_sources") or None,
    )
    base = {"user_id": user_id, "canonical_user_id": str(canonical), "item_type": item_type}
    if not candidates:
        return {**base, "recommendations": [], "note": "cold-start: no taste vector yet"}

    domain_facts = [f for f in facts if f["domain"] in ("general", item_type)]
    scored = apply_knowledge([score_candidate(c) for c in candidates], domain_facts, overlay.get("source_weights") or {})
    final = rerank(scored, limit=limit, explore_ratio=explore_ratio)

    return {
        **base,
        "recommendations": [
            {
                "source": c.source,
                "source_item_id": c.source_item_id,
                "title": c.title,
                "genres": c.genres,
                "score": round(c.score, 4),
                "confidence": round(c.confidence, 4),
                "explore": c.confidence < 0.5,
                "explanation": {k: round(v, 4) for k, v in c.reasons.items()},
                "why": describe(c, domain_facts),
            }
            for c in final
        ],
    }


@app.get("/soon-gone")
async def soon_gone_list() -> dict:
    """Backs the Jellyfin plugin's "Soon Gone" homepage row."""
    pool = await get_pool()
    items = await soon_gone_repo.list_pending(pool)
    return {"items": items}


@app.post("/soon-gone/{source_item_id}/whitelist")
async def soon_gone_whitelist(source_item_id: str, user_id: str = Query(...)) -> dict:
    """Called by the Jellyfin plugin's playback-start hook — opening a "Soon Gone" item
    saves it permanently (see docs/architecture.md). Idempotent: whitelisting an item
    that isn't pending (already saved, expired, or never marked) is a no-op.
    """
    pool = await get_pool()
    whitelisted = await soon_gone_repo.whitelist(pool, source_item_id, user_id)
    return {"source_item_id": source_item_id, "whitelisted": whitelisted}
