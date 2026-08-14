"""Recommendation API — implements the 3-stage funnel from docs/architecture.md:
candidate generation (candidates.py) -> ranking (scoring.score_candidate) ->
re-ranking (scoring.rerank). Explainability comes for free: `reasons` on every
scored candidate is exactly which weighted factors produced its score.
"""

import os

from fastapi import FastAPI, HTTPException, Query
from qdrant_client import QdrantClient

from .candidates import generate_candidates
from .db import close_pool, get_pool
from .scoring import score_candidate, rerank
from . import soon_gone as soon_gone_repo

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
ITEM_TYPES = ["movie", "series_episode", "youtube_video", "track", "audiobook", "ebook"]

app = FastAPI(title="Muse Recommendation API", version="0.1.0")
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


@app.get("/recommendations/{user_id}")
async def recommendations(
    user_id: str,
    item_type: str = Query(..., description=f"one of {ITEM_TYPES}"),
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0.0, le=1.0, description="overrides the default explore-slot ratio"),
) -> dict:
    if item_type not in ITEM_TYPES:
        raise HTTPException(status_code=400, detail=f"item_type must be one of {ITEM_TYPES}")

    pool = await get_pool()
    candidates = await generate_candidates(pool, qdrant(), user_id, item_type)
    if not candidates:
        return {"user_id": user_id, "item_type": item_type, "recommendations": [], "note": "cold-start: no taste vector yet"}

    scored = [score_candidate(c) for c in candidates]
    kwargs = {"explore_ratio": explore_ratio} if explore_ratio is not None else {}
    final = rerank(scored, limit=limit, **kwargs)

    return {
        "user_id": user_id,
        "item_type": item_type,
        "recommendations": [
            {
                "source": c.source,
                "source_item_id": c.source_item_id,
                "score": round(c.score, 4),
                "confidence": round(c.confidence, 4),
                "explanation": {k: round(v, 4) for k, v in c.reasons.items()},
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
