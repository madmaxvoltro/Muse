"""Recommendation API (read side). See docs/recommender.md for the pipeline and the research
behind each stage.

  GET  /recommendations/{user_id}       ranked, explained, calibrated recommendations
  GET  /rows/{user_id}                  a home screen of themed, explained rows (no duplicates)
  GET  /profile/{user_id}               the taste profile: interests, summary, what Muse learned
  GET  /evolution/{user_id}             how your taste is changing: timeline, lifecycle, rhythms
  GET  /questions/{user_id}/queue       background-filled question queue (optional to use)
  GET  /questions/{user_id}/next        the single most useful question to ask right now
  GET  /questions/{user_id}/onboarding  diverse well-known titles to tap ("which do you love?")
  GET  /questions/{user_id}/this-or-that a pairwise question where Muse is least sure
  GET  /questions/{user_id}/abandoned   titles stopped early, to ask why
  GET  /soon-gone, POST /soon-gone/{id}/whitelist   curator grace window (unchanged)

Multi-space engine (routes_multispace.py): /next, /radio, /group-recommendations, /fit,
/explain/{user}/{source}/{id}, /context; /recommendations dispatches to it for music, YouTube and
books and for requests naming a source, room, device or another domain's taste.

Answers to questions go to the ingestion API (POST /answers): this service never writes
taste data, only impressions and question bookkeeping.
"""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from muse_schema.config import key_list, secret
from muse_schema.crypto import get_vault
from qdrant_client import QdrantClient

from . import questions, rows, status_page
from . import routes_multispace as MS
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


app = FastAPI(title="Muse Recommendation API", version="0.3.0", dependencies=[Depends(require_read_key)], lifespan=lifespan)
app.include_router(MS.router)  # /next, /radio, /group-recommendations, /fit, /explain/{u}/{source}/{id}, /context


def _check_type(item_type: str) -> None:
    if item_type not in TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"item_type must be one of {ITEM_TYPES}")


@app.get("/status", include_in_schema=False)
async def status(request: Request):
    """Read-only status page (counts only). ./muse status --url prints the link."""
    return await status_page.page(request, await get_pool())


@app.get("/status.json", include_in_schema=False)
async def status_json():
    return await status_page.page_json(await get_pool())


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Domains the multi-space engine serves (sound, sessions, sequencing); films and series go to
# the taste engine unless the request asks for something only the multi-space engine does.
MULTISPACE_TYPES = {"track", "youtube_video", "audiobook", "ebook"}


@app.get("/recommendations/{user_id}")
async def recommendations(
    user_id: str,
    item_type: str = Query(..., description=f"one of {ITEM_TYPES}"),
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0.0, le=1.0, description="share of explore slots; default from the user overlay (12.5%)"),
    mood: str | None = Query(None, max_length=200, description="a preset (focus, chill, workout, ...) or free text, e.g. 'cosy and funny'"),
    surface: str = Query("home", max_length=40, description="where the list is shown; separate rotation and impression stats per surface"),
    include_unavailable: bool = Query(False, description="also titles not in the library (discovery / curator)"),
    log: bool = Query(True, description="log impressions (turn off for previews)"),
    at: datetime | None = Query(None, description="ISO time to use as 'now' for rhythms (default: now)"),
    # multi-space engine (music lists, context-aware lists)
    source: list[str] | None = Query(None, description="only items from these sources, e.g. source=mono (playable) or source=catalog (discovery)"),
    taste_from: str | None = Query(None, description="rank with another domain's taste"),
    blend: float | None = Query(None, ge=0.0, le=1.0, description="share of cross-domain taste mixed in"),
    tz: str | None = Query(None, description="IANA time zone of the person, for time-of-day context"),
    device: str | None = Query(None, description="device name, e.g. 'Living room TV' (context)"),
    device_class: str | None = Query(None, description="phone | desktop | tv | car | speaker | tablet"),
    room: str | None = Query(None, description="room for room-level context (Home Assistant)"),
    use_context: bool = Query(True, description="apply stored context signals (mood, activity, weather, calendar)"),
    engine_name: str | None = Query(None, alias="engine", description="taste | multispace (default: chosen by item type and parameters)"),
) -> dict:
    """Ranked, explained recommendations. Films and series come from the taste engine
    (canonical titles, calibrated lists); music, YouTube and books, and any request that names a
    source, room, device or another domain's taste, from the multi-space engine. Both answer
    with `why` (readable reasons) and `explanation` (weighted terms)."""
    _check_type(item_type)
    if engine_name not in (None, "taste", "multispace"):
        raise HTTPException(status_code=400, detail="engine must be taste or multispace")
    wants_multi = engine_name == "multispace" or (
        engine_name is None and (item_type in MULTISPACE_TYPES or bool(source or taste_from or room or device or device_class or tz)))

    async def multi() -> dict:
        return await MS.recommendations(user_id, item_type, limit, explore_ratio, source, taste_from, blend, mood, tz, device,
                                        device_class, room, use_context, surface, log)

    if wants_multi:
        return {**await multi(), "engine": "multispace"}
    eng = await engine()
    if not await asyncio.to_thread(eng.catalog_ready):
        building = {"user_id": user_id, "item_type": item_type, "recommendations": [],
                    "note": "Muse is still building its catalog (first start); see ./muse status"}
        if engine_name == "taste":
            return building
        try:  # the multi-space engine may already have vectors for this person
            res = await multi()
        except Exception:  # noqa: BLE001 - nothing built yet anywhere: same graceful answer
            return building
        if not res.get("recommendations"):
            return {**building, "engine": "multispace"}
        return {**res, "engine": "multispace", "note": "taste engine catalog not built yet"}
    try:
        res = await eng.recommend(RecoRequest(user_id, item_type, limit, explore_ratio, mood, surface, include_unavailable, log, at))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    recs = []
    for s in res.items:
        d = eng.describe(s, res.profile, res.titles)
        d["why"] = d.get("reasons", [])
        recs.append(d)
    body = {
        "user_id": user_id,
        "item_type": item_type,
        "engine": "taste",
        "request_id": res.request_id,
        "recommendations": recs,
    }
    if res.cold_start:
        body["note"] = "cold-start: no taste profile yet, showing well-rated titles; ask /questions/{user_id}/next"
        body["onboarding_suggested"] = True
    return body


@app.get("/rows/{user_id}")
async def home_rows(
    user_id: str,
    item_type: str = Query(..., description=f"one of {ITEM_TYPES}"),
    per_row: int = Query(12, ge=4, le=40),
    max_rows: int = Query(8, ge=1, le=15),
    at: datetime | None = Query(None, description="ISO time to use as 'now' for rhythms"),
    log: bool = Query(True),
) -> dict:
    """A home screen: themed rows whose titles are their explanations (Top picks, Because you
    watched X, More <interest>, For your weekend evenings, Watch again, From your watchlist,
    New in your library, Something different). A title appears once per page."""
    _check_type(item_type)
    eng = await engine()
    if not await asyncio.to_thread(eng.catalog_ready):
        return {"user_id": user_id, "item_type": item_type, "rows": [], "note": "Muse is still building its catalog (first start)"}
    return await rows.build(eng, user_id, item_type, per_row, max_rows, at, log)


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
    eng = await engine()
    if not await asyncio.to_thread(eng.catalog_ready):
        return {"question_id": None, "kind": None, "note": "catalog not built yet"}
    return await questions.next_question(eng, user_id, item_type)


@app.get("/questions/{user_id}/onboarding")
async def question_onboarding(user_id: str, item_type: str = Query("movie"), n: int = Query(24, ge=4, le=60)) -> dict:
    _check_type(item_type)
    eng = await engine()
    if not await asyncio.to_thread(eng.catalog_ready):
        return {"question_id": None, "kind": "onboarding", "items": [], "note": "catalog not built yet"}
    return await questions.onboarding(eng, user_id, item_type, n)


@app.get("/questions/{user_id}/this-or-that")
async def question_pair(user_id: str, item_type: str = Query("movie")) -> dict:
    _check_type(item_type)
    eng = await engine()
    if not await asyncio.to_thread(eng.catalog_ready):
        return {"question_id": None, "kind": "this_or_that", "note": "catalog not built yet"}
    return await questions.pairwise(eng, user_id, item_type)


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
