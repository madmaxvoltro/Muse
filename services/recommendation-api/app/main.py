"""Recommendation API (read side). See docs/recommender.md for the pipeline and the research
behind each stage.

  GET  /recommendations/{user_id}       ranked, explained, calibrated recommendations
  GET  /profile/{user_id}               the taste profile: interests, summary, what Muse learned
  GET  /evolution/{user_id}             how your taste is changing: timeline, lifecycle, rhythms
  GET  /questions/{user_id}/queue       background-filled question queue (optional to use)
  GET  /questions/{user_id}/next        the single most useful question to ask right now
  GET  /questions/{user_id}/onboarding  diverse well-known titles to tap ("which do you love?")
  GET  /questions/{user_id}/this-or-that a pairwise question where Muse is least sure
  GET  /questions/{user_id}/abandoned   titles stopped early, to ask why
  GET  /soon-gone, POST /soon-gone/{id}/whitelist   curator grace window (unchanged)

Answers to questions go to the ingestion API (POST /answers): this service never writes
taste data, only impressions and question bookkeeping.
"""

import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from muse_schema.config import key_list, secret
from muse_schema.crypto import get_vault
from qdrant_client import QdrantClient

from . import questions
from . import soon_gone as soon_gone_repo
from .db import close_pool, get_pool
from .engine import TARGET_TYPES, Engine, Request as RecoRequest

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
ITEM_TYPES = list(TARGET_TYPES)

# MUSE_READ_KEYS_FILE (a Docker secret, one "client:key" per line, e.g. vesper:..., jellyfin-plugin:...)
# or MUSE_READ_KEYS (comma separated, development). Callers send X-Muse-Read-Key. Read keys can
# only read; ingestion keys can only write. Secure by default: with no read keys configured
# every request is refused, unless MUSE_ALLOW_OPEN_READ=true (development only).
_READ_KEYS = key_list("MUSE_READ_KEYS")
_PUBLIC = {"/healthz"}


def _open_read_allowed() -> bool:
    return os.environ.get("MUSE_ALLOW_OPEN_READ", "false").lower() in ("1", "true", "yes")


async def require_read_key(request: Request, x_muse_read_key: str | None = Header(default=None)) -> None:
    if request.url.path in _PUBLIC or (request.url.path.startswith("/status") and _status_token_ok(request)):
        return
    if not _READ_KEYS:
        if _open_read_allowed():
            return
        raise HTTPException(status_code=503, detail="no read keys configured (MUSE_READ_KEYS_FILE); run ./muse setup")
    supplied = (x_muse_read_key or "").encode()
    if not any(hmac.compare_digest(k.encode(), supplied) for k in _READ_KEYS):
        raise HTTPException(status_code=401, detail="invalid or missing X-Muse-Read-Key")


def _status_token_ok(request: Request) -> bool:
    token = secret("MUSE_STATUS_TOKEN")
    supplied = request.query_params.get("token") or request.cookies.get("muse_status") or ""
    return bool(token) and hmac.compare_digest(token.encode(), supplied.encode())


def _embedder_factory():
    # Only needed for ?mood= queries; loaded lazily on first use.
    from muse_taste import embedder

    return embedder.from_env()


_engine: Engine | None = None


async def engine() -> Engine:
    global _engine
    if _engine is None:
        # read-only Qdrant key: this service can search vectors but never change them
        _engine = Engine(await get_pool(), QdrantClient(url=QDRANT_URL, api_key=secret("MUSE_QDRANT_API_KEY") or None), _embedder_factory)
    return _engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_vault()  # fail fast without the master key
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
    at: datetime | None = Query(None, description="ISO time to use as 'now' for rhythms (default: now)"),
) -> dict:
    _check_type(item_type)
    eng = await engine()
    try:
        res = await eng.recommend(RecoRequest(user_id, item_type, limit, explore_ratio, mood, surface, include_unavailable, log, at))
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
    titles = await eng.titles_for(keys)
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


@app.get("/evolution/{user_id}")
async def evolution(user_id: str, domain: str = Query("video", description="video | youtube | music | books")) -> dict:
    """How your taste is changing (muse_taste.evolution): per interest its lifecycle state,
    trend, satiation, rediscovery pull and rhythms; a monthly timeline with breadth and drift;
    milestones; how far Muse currently stretches; and (with an LLM) a short narrative."""
    eng = await engine()
    prof = await eng.profile(user_id, domain)
    if prof is None or not prof.evolution:
        raise HTTPException(status_code=404, detail="no evolution report yet (needs a profile with some history)")
    ev = prof.evolution
    keys = list({i.medoid_key for i in prof.interests} | {m for i in prof.interests for m in i.members[:4]})
    titles = await eng.titles_for(keys)

    def name(k: int) -> str:
        it = prof.interests[k]
        return it.label or ", ".join(t.split(":", 1)[-1] for t in it.top_tags[:2]) or titles.get(it.medoid_key, f"interest {k + 1}")

    interests = []
    for ks, info in ev.get("interests", {}).items():
        k = int(ks)
        if k >= len(prof.interests):
            continue
        ctx = info.get("context") or {}
        peaks = [f"{b}" for part in ("daypart", "weekpart", "season") for b, lift in (ctx.get(part) or {}).items() if lift >= 1.3]
        interests.append({
            "id": k,
            "name": name(k),
            "state": info.get("state"),
            "trend": info.get("trend"),
            "satiation": info.get("satiation"),
            "rediscovery": info.get("rediscovery"),
            "first_seen": info.get("first_seen"),
            "last_seen": info.get("last_seen"),
            "share_now": round(prof.interests[k].importance, 4),
            "lifetime_share": info.get("lifetime_share"),
            "usually": peaks,  # e.g. ["evening", "weekend"]: when you tend to pick this
            "rhythms": ctx,
            "examples": [titles.get(m, m) for m in prof.interests[k].members[:4]],
        })
    names = {str(i["id"]): i["name"] for i in interests}
    timeline = [{**row, "shares": {names.get(k, "other" if k == "-1" else k): v for k, v in row["shares"].items()}} for row in ev.get("timeline", [])]
    return {
        "user_id": user_id,
        "domain": domain,
        "computed_at": ev.get("computed_at"),
        "timezone": ev.get("timezone"),
        "narrative": ev.get("narrative"),
        "interests": sorted(interests, key=lambda i: -(i["share_now"] or 0)),
        "timeline": timeline,
        "milestones": ev.get("milestones", []),
        "stretch": ev.get("stretch", {}),
    }


@app.get("/questions/{user_id}/queue")
async def question_queue(
    user_id: str,
    limit: int = Query(5, ge=1, le=50),
    domain: str | None = Query(None, description="video | youtube | music | books"),
    kind: list[str] | None = Query(None, description="onboarding | abandon | interest | this_or_that (repeatable)"),
    mark_served: bool = Query(True, description="mark returned pending questions as served"),
    include_served: bool = Query(True, description="also return questions served earlier and not answered yet"),
) -> dict:
    """Questions the worker queued in the background, most valuable first. Each entry includes
    the exact body to POST to the ingestion API's /answers. Optional: nothing waits for it."""
    return await questions.queue(await engine(), user_id, limit, domain, kind, mark_served, include_served)


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
