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
from muse_schema.crypto import get_vault, item_key
from qdrant_client import QdrantClient

from muse_schema import ratings

from . import prefetch as prefetch_mod, questions, rows, status_page, wrapped as wrapped_mod
from . import taste_sequencing as tseq
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
    max_age: int | None = Query(None, ge=0, le=21, description="kids profiles: only titles rated for this age or younger"),
    allow_unrated: bool = Query(False, description="with max_age: also titles without a known rating"),
) -> dict:
    """Ranked, explained recommendations. Films and series come from the taste engine
    (canonical titles, calibrated lists); music, YouTube and books, and any request that names a
    source, room, device or another domain's taste, from the multi-space engine. Both answer
    with `why` (readable reasons) and `explanation` (weighted terms)."""
    if max_age is None:
        return await _recommend(user_id, item_type, limit, explore_ratio, mood, surface, include_unavailable, log, at, source,
                                taste_from, blend, tz, device, device_class, room, use_context, engine_name)
    # Over-fetch, then keep what the profile may see (unknown ratings only with allow_unrated).
    body = await _recommend(user_id, item_type, min(100, limit * 3), explore_ratio, mood, surface, include_unavailable, False, at,
                            source, taste_from, blend, tz, device, device_class, room, use_context, engine_name)
    body["recommendations"] = filter_age(body.get("recommendations") or [], max_age, allow_unrated)[:limit]
    body["max_age"] = max_age
    return body


def filter_age(items: list[dict], max_age: int | None, allow_unrated: bool = False) -> list[dict]:
    return [r for r in items if ratings.within(r.get("certification") or r.get("official_rating"), max_age, allow_unrated)]


async def _recommend(user_id, item_type, limit, explore_ratio, mood, surface, include_unavailable, log, at, source, taste_from,
                     blend, tz, device, device_class, room, use_context, engine_name) -> dict:
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


# --- music on the taste engine (fallback for /next and /radio) -----------------------------

async def _canon_for(eng: Engine, refs: list[str]) -> list[str]:
    """'source:id' (real ids, as players know them) -> blind canonical keys, in order."""
    vault = get_vault()
    keys = []
    for r in refs:
        if ":" not in r:
            continue
        source, sid = r.split(":", 1)
        keys.append(item_key(vault, source, sid))
    if not keys:
        return []
    async with eng.pool.acquire() as conn:
        rows_ = await conn.fetch("SELECT item_key, canon_key FROM item WHERE item_key = ANY($1::text[])", keys)
    by = {r["item_key"]: r["canon_key"] for r in rows_}
    return [by[k] for k in keys if k in by]


async def _track_pool(eng: Engine, user_id: str, discovery: float, exclude: set[str]) -> tuple[list[tseq.Track], object]:
    """Unheard songs the engine likes for this person, plus their favourites (a radio plays those too)."""
    res = await eng.recommend(RecoRequest(user_id, "track", limit=100, explore_ratio=0.0, surface="radio", log=False))
    pool = [s for s in res.pool[:300] if s.key not in exclude]
    top = max((s.score for s in pool), default=1.0) or 1.0
    tracks = [tseq.Track(s.key, s.vector, s.payload, max(0.0, min(1.0, s.score / top)) * (0.7 + 0.3 * discovery), tseq.artist_of(s.payload))
              for s in pool]
    prof = res.profile
    # Songs score lower than films per listen (muse_taste.signals: a few full listens ~ 0.5), so
    # anything clearly liked counts as a favourite here.
    loved = [a for a in (prof.anchors if prof else []) if a.preference > 0.35 and a.key not in exclude][:120]
    if loved and discovery < 1.0:
        got: dict = {}
        await asyncio.to_thread(eng._by_keys, [a.key for a in loved], ["track"], False, got)
        for a in loved:
            if a.key in got:
                vec, pl = got[a.key]
                taste = (0.6 + 0.4 * min(a.preference, 1.0)) * (1.0 - 0.6 * discovery)
                tracks.append(tseq.Track(a.key, vec, pl, taste, tseq.artist_of(pl)))
    return tracks, prof


async def _seed_track(eng: Engine, ck: str) -> tseq.Track | None:
    got: dict = {}
    await asyncio.to_thread(eng._by_keys, [ck], ["track"], True, got)
    if ck not in got:
        return None
    vec, pl = got[ck]
    return tseq.Track(ck, vec, pl, 0.5, tseq.artist_of(pl))


def _track_json(t: tseq.Track, score: float, parts: dict, prev: tseq.Track, playable: list[str] | None) -> dict | None:
    refs = t.payload.get("library_refs") or []
    if playable:
        refs = [r for r in refs if r.get("source") in playable]
    if not refs:
        return None
    return {"source": refs[0]["source"], "source_item_id": refs[0]["source_item_id"], "canon_key": t.key, "title": t.title,
            "transition": {"score": round(score, 4), **{k: round(v, 3) for k, v in parts.items()}}, "why": tseq.why(prev, parts)}


async def taste_next_up(
    user_id: str,
    source: str,
    source_item_id: str,
    n: int = 5,
    history: list[str] | None = None,
    discovery: float = 0.3,
    playable_source: list[str] | None = None,
) -> dict:
    """What should follow this song, from the taste engine: fits the person's taste and flows
    from it (closeness of item vectors, artist spacing). /next uses it when the multi-space
    engine has nothing for the song yet (no audio analysis or behaviour vectors)."""
    eng = await engine()
    cur_keys = await _canon_for(eng, [f"{source}:{source_item_id}"])
    if not cur_keys:
        raise HTTPException(404, "Muse doesn't know that song yet")
    cur = await _seed_track(eng, cur_keys[0])
    if cur is None:
        raise HTTPException(404, "that song isn't in the catalog yet")
    exclude = set(await _canon_for(eng, history or [])) | {cur.key}
    tracks, _ = await _track_pool(eng, user_id, discovery, exclude)
    hist_artists = [cur.artist]
    picks = tseq.next_tracks(cur, tracks, n * 3, hist_artists)
    out = [j for t, s, parts in picks if (j := _track_json(t, s, parts, cur, playable_source))][:n]
    return {"user_id": user_id, "after": {"canon_key": cur.key, "title": cur.title}, "next": out}


async def taste_radio(
    user_id: str,
    seed_source: str | None = None,
    seed_id: str | None = None,
    length: int = 25,
    discovery: float = 0.3,
    playable_source: list[str] | None = None,
) -> dict:
    """A listening session from the taste engine (beam search, favourites as radio material);
    /radio uses it when the multi-space engine has nothing to play yet."""
    eng = await engine()
    seed = None
    if seed_source and seed_id:
        keys = await _canon_for(eng, [f"{seed_source}:{seed_id}"])
        seed = await _seed_track(eng, keys[0]) if keys else None
        if seed is None:
            raise HTTPException(404, "Muse doesn't know that song yet")
    tracks, _ = await _track_pool(eng, user_id, discovery, {seed.key} if seed else set())
    if playable_source:
        tracks = [t for t in tracks if any(r.get("source") in playable_source for r in t.payload.get("library_refs") or [])]
    if not tracks:
        return {"user_id": user_id, "tracks": [], "note": "nothing to play yet"}
    lead = []
    if seed is None:
        seed = max(tracks, key=lambda t: t.taste)
        tracks = [t for t in tracks if t.key != seed.key]
        lead = [j for j in [_track_json(seed, seed.taste, {"taste": seed.taste}, seed, playable_source)] if j]
    session = tseq.radio(seed, tracks, length - len(lead))
    out, prev = list(lead), seed
    for t, s, parts in session:
        j = _track_json(t, s, parts, prev, playable_source)
        if j:
            out.append(j)
        prev = t
    return {"user_id": user_id, "seed": seed.key, "tracks": out}


@app.get("/rows/{user_id}")
async def home_rows(
    user_id: str,
    item_type: str = Query(..., description=f"one of {ITEM_TYPES}"),
    per_row: int = Query(12, ge=4, le=40),
    max_rows: int = Query(8, ge=1, le=15),
    at: datetime | None = Query(None, description="ISO time to use as 'now' for rhythms"),
    log: bool = Query(True),
    max_age: int | None = Query(None, ge=0, le=21, description="kids profiles: only titles rated for this age or younger"),
    allow_unrated: bool = Query(False),
) -> dict:
    """A home screen: themed rows whose titles are their explanations (Top picks, Because you
    watched X, More <interest>, For your weekend evenings, Watch again, From your watchlist,
    New in your library, Something different). A title appears once per page."""
    _check_type(item_type)
    eng = await engine()
    if not await asyncio.to_thread(eng.catalog_ready):
        return {"user_id": user_id, "item_type": item_type, "rows": [], "note": "Muse is still building its catalog (first start)"}
    res = await rows.build(eng, user_id, item_type, per_row, max_rows, at, log and max_age is None)
    if max_age is not None:
        for row in res.get("rows") or []:
            row["items"] = filter_age(row.get("items") or [], max_age, allow_unrated)
        res["rows"] = [r for r in res.get("rows") or [] if r["items"]]
        res["max_age"] = max_age
    return res


@app.get("/wrapped/{user_id}")
async def wrapped(user_id: str, year: int | None = Query(None, ge=2000, le=2100), tz: str | None = None, kids: bool = False) -> dict:
    """A person's year in films and series (Vesper Wrapped): minutes, films, binge days, top
    titles, genres, when they watch. kids=true leaves out the time statistics."""
    from datetime import timezone as _tz

    y = year or datetime.now(_tz.utc).year
    zone = prefetch_mod.tzinfo(tz)
    uid, events = await wrapped_mod.load(await get_pool(), user_id, y, zone)
    return {"user_id": uid, "year": y, **wrapped_mod.aggregate(events, zone, kids)}


@app.get("/why/{user_id}/{source}/{source_item_id}")
async def why(user_id: str, source: str, source_item_id: str, item_type: str = Query("movie")) -> dict:
    """Why this title for this person, from the taste engine (the same reasons a list would
    give): for a title screen, whether or not it is in a list right now."""
    _check_type(item_type)
    eng = await engine()
    empty = {"user_id": user_id, "source": source, "source_item_id": source_item_id, "reasons": [], "reason_details": []}
    if not await asyncio.to_thread(eng.catalog_ready):
        return empty
    res = await eng.recommend(RecoRequest(user_id, item_type, limit=100, explore_ratio=0.0, surface="detail", log=False))
    for s in res.pool:
        refs = s.payload.get("library_refs") or []
        if any(r.get("source") == source and r.get("source_item_id") == source_item_id for r in refs):
            d = eng.describe(s, res.profile, res.titles)
            return {"user_id": user_id, **d, "source": source, "source_item_id": source_item_id, "seen": False}
    return {**empty, "seen": None}


@app.get("/prefetch/{user_id}")
async def prefetch(
    user_id: str,
    horizon_hours: int = Query(24, ge=1, le=168),
    tz: str | None = Query(None, description="IANA time zone of the household, e.g. Europe/Amsterdam"),
    limit: int = Query(10, ge=1, le=50),
    max_age: int | None = Query(None, ge=0, le=21),
) -> dict:
    """What this person will likely start in the next hours (in-progress titles, the series they
    follow, a few top picks), each with a likelihood: Vesper promotes these to fast storage and
    to smart downloads on phones."""
    zone = prefetch_mod.tzinfo(tz)
    now = prefetch_mod.now_utc()
    uid, acts, history = await prefetch_mod.load(await get_pool(), user_id)
    p_active = prefetch_mod.active_probability(prefetch_mod.slot_rates(acts, zone, now), now, horizon_hours, zone)
    items = prefetch_mod.rank_items(history, now)
    if len(items) < limit:
        try:
            recs = await _recommend(uid, "movie", limit * 3, 0.0, None, "prefetch", False, False, None, None, None, None, tz,
                                    None, None, None, True, "taste")
            for r in filter_age(recs.get("recommendations") or [], max_age)[:3]:
                if r.get("available") is False:
                    continue
                items.append({"source": r["source"], "source_item_id": r["source_item_id"], "item_type": r.get("item_type") or "movie",
                              "title": r.get("title") or "", "reason": "recommended", "base": 0.25 * float(r.get("confidence") or 0.5)})
        except Exception:  # noqa: BLE001 - prefetch must not fail on a cold profile or an unbuilt catalog
            pass
    for it in items:
        it["likelihood"] = round(it.pop("base") * max(p_active, 0.15), 4)
    items.sort(key=lambda x: -x["likelihood"])
    return {"user_id": uid, "horizon_hours": horizon_hours, "active_probability": round(p_active, 4), "items": items[:limit]}


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


# /next and /radio fall back to the taste engine when the multi-space engine has nothing yet.
async def _next_fallback(**kw):
    if not await asyncio.to_thread((await engine()).catalog_ready):
        return None
    return await taste_next_up(**kw)


async def _radio_fallback(**kw):
    if not await asyncio.to_thread((await engine()).catalog_ready):
        return None
    return await taste_radio(**kw)


MS.TASTE_FALLBACK.update(next=_next_fallback, radio=_radio_fallback)
