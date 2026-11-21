"""Recommendation API (read side). See docs/recommender.md for the pipeline and the research
behind each stage.

  GET  /recommendations/{user_id}       ranked, explained, calibrated recommendations
  GET  /profile/{user_id}               the taste profile: interests, summary, what Muse learned
  GET  /questions/{user_id}/next        the single most useful question to ask right now
  GET  /questions/{user_id}/onboarding  diverse well-known titles to tap ("which do you love?")
  GET  /questions/{user_id}/this-or-that a pairwise question where Muse is least sure
  GET  /questions/{user_id}/abandoned   titles stopped early, to ask why
  GET  /soon-gone, POST /soon-gone/{id}/whitelist   curator grace window (unchanged)

Answers to questions go to the ingestion API (POST /answers): this service never writes
taste data, only impressions and question bookkeeping.
"""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from qdrant_client import QdrantClient

from . import questions
from . import soon_gone as soon_gone_repo
from .db import close_pool, get_pool
from .engine import TARGET_TYPES, Engine, Request as RecoRequest

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
ITEM_TYPES = list(TARGET_TYPES)

# MUSE_READ_KEYS="vesper:xxxx,jellyfin-plugin:yyyy": callers must send X-Muse-Read-Key. Read
# keys can only read; ingestion keys (MUSE_ADAPTER_KEYS) can only write. Unset = open (dev).
_READ_KEYS = {k.split(":", 1)[1] for k in os.environ.get("MUSE_READ_KEYS", "").split(",") if ":" in k}


async def require_read_key(request: Request, x_muse_read_key: str | None = Header(default=None)) -> None:
    if request.url.path == "/healthz":
        return
    if _READ_KEYS and x_muse_read_key not in _READ_KEYS:
        raise HTTPException(status_code=401, detail="invalid or missing X-Muse-Read-Key")


def _embedder_factory():
    # Only needed for ?mood= queries; loaded lazily on first use.
    from muse_taste import embedder

    return embedder.from_env()


_engine: Engine | None = None


async def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine(await get_pool(), QdrantClient(url=QDRANT_URL), _embedder_factory)
    return _engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await close_pool()


app = FastAPI(title="Muse Recommendation API", version="0.2.0", dependencies=[Depends(require_read_key)], lifespan=lifespan)


def _check_type(item_type: str) -> None:
    if item_type not in TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"item_type must be one of {ITEM_TYPES}")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/recommendations/{user_id}")
async def recommendations(
    user_id: str,
    item_type: str = Query(..., description=f"one of {ITEM_TYPES}"),
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0.0, le=1.0, description="share of explore slots; default from the user overlay (12.5%)"),
    mood: str | None = Query(None, max_length=200, description="free text, e.g. 'cosy and funny' (needs the embedder in this service)"),
    surface: str = Query("home", max_length=40, description="where the list is shown; separate rotation and impression stats per surface"),
    include_unavailable: bool = Query(False, description="also titles not in the library (discovery / curator)"),
    log: bool = Query(True, description="log impressions (turn off for previews)"),
) -> dict:
    _check_type(item_type)
    eng = await engine()
    try:
        res = await eng.recommend(RecoRequest(user_id, item_type, limit, explore_ratio, mood, surface, include_unavailable, log))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = {
        "user_id": user_id,
        "item_type": item_type,
        "request_id": res.request_id,
        "recommendations": [eng.describe(s, res.profile, res.titles) for s in res.items],
    }
    if res.cold_start:
        body["note"] = "cold-start: no taste profile yet, showing well-rated titles; ask /questions/{user_id}/next"
        body["onboarding_suggested"] = True
    return body


@app.get("/profile/{user_id}")
async def profile(user_id: str, domain: str = Query("video", description="video | youtube | music | books")) -> dict:
    eng = await engine()
    prof = await eng.profile(user_id, domain)
    if prof is None:
        raise HTTPException(status_code=404, detail="no profile for this user/domain yet")
    keys = list({m for i in prof.interests for m in i.members[:6]} | {n.key for n in prof.negatives[:10]} | {a.key for a in prof.anchors[:15]})
    async with eng.pool.acquire() as conn:
        titles = {r["canon_key"]: r["title"] for r in await conn.fetch("SELECT canon_key, title FROM canon_item WHERE canon_key = ANY($1::text[])", keys)}
    return {
        "user_id": user_id,
        "domain": domain,
        "computed_at": prof.computed_at,
        "summary": prof.summary,
        "interests": [
            {
                "id": i.id,
                "label": i.label,
                "kind": i.kind,
                "share": round(i.importance, 4),
                "size": i.size,
                "top_tags": [t.split(":", 1)[-1] for t in i.top_tags],
                "examples": [titles.get(m, m) for m in i.members[:6]],
            }
            for i in prof.interests
        ],
        "top_titles": [{"title": titles.get(a.key, a.key), "preference": round(a.preference, 2)} for a in prof.anchors[:15] if a.preference > 0],
        "disliked": [titles.get(n.key, n.key) for n in prof.negatives[:10]],
        "top_tags": sorted(((t, w) for t, w in prof.tag_weights.items() if w > 0), key=lambda kv: -kv[1])[:20],
        "stats": {"liked": prof.n_positive, "disliked": prof.n_negative, "evidence": round(prof.total_confidence, 1),
                  "seen": len(prof.seen), "watchlist": len(prof.backlog), "model": prof.model},
    }


@app.get("/questions/{user_id}/next")
async def question_next(user_id: str, item_type: str = Query("movie")) -> dict:
    _check_type(item_type)
    return await questions.next_question(await engine(), user_id, item_type)


@app.get("/questions/{user_id}/onboarding")
async def question_onboarding(user_id: str, item_type: str = Query("movie"), n: int = Query(24, ge=4, le=60)) -> dict:
    _check_type(item_type)
    return await questions.onboarding(await engine(), user_id, item_type, n)


@app.get("/questions/{user_id}/this-or-that")
async def question_pair(user_id: str, item_type: str = Query("movie")) -> dict:
    _check_type(item_type)
    return await questions.pairwise(await engine(), user_id, item_type)


@app.get("/questions/{user_id}/abandoned")
async def question_abandoned(user_id: str, item_type: str = Query("movie")) -> dict:
    _check_type(item_type)
    return await questions.abandoned(await engine(), user_id, item_type)


@app.get("/soon-gone")
async def soon_gone_list() -> dict:
    """Backs the Jellyfin plugin's "Soon Gone" homepage row."""
    pool = await get_pool()
    items = await soon_gone_repo.list_pending(pool)
    return {"items": items}


@app.post("/soon-gone/{source_item_id}/whitelist")
async def soon_gone_whitelist(source_item_id: str, user_id: str = Query(...)) -> dict:
    """Called by the Jellyfin plugin's playback-start hook — opening a "Soon Gone" item
    saves it permanently (see docs/architecture.md). Idempotent."""
    pool = await get_pool()
    whitelisted = await soon_gone_repo.whitelist(pool, source_item_id, user_id)
    return {"source_item_id": source_item_id, "whitelisted": whitelisted}
