"""Recommendation API — the read side of Muse for players and apps.

GET /recommendations/{user}     ranked list (engine.py: many generators, every taste space,
                                facets, context and mood, facts, explore slots)
GET /next/{user}                what should play after a given item (sequencing.py)
GET /radio/{user}               a whole session from a seed, following an energy arc
GET /group-recommendations      several people at once, fair and with vetoes (groups.py)
GET /fit/{source}/{id}          how well one item fits each of several people

Explainability: `explanation` holds every weighted term of a score, `why` the readable version.
"""

import json
import os
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from muse_schema import identity_group
from qdrant_client import QdrantClient

from . import engine as E
from . import groups as G
from . import sequencing as S
from . import soon_gone as soon_gone_repo
from .context import effective_context
from .db import close_pool, get_pool
from .impressions import log_impressions
from .ranker import weights_for
from .scoring import EXPLORE_RATIO_DEFAULT, apply_knowledge

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


app = FastAPI(title="Muse Recommendation API", version="0.2.0", dependencies=[Depends(require_read_key)])
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
                    "SELECT kind, domain, subject, sentiment, confidence, statement FROM profile_fact "
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


def _uuid(v: str) -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except ValueError:
        raise HTTPException(status_code=400, detail="user ids must be UUIDs")


def _item_json(it: E.Item, facts, ctx, pref_summary=None) -> dict:
    out = {
        "source": it.source,
        "source_item_id": it.source_item_id,
        "title": it.title,
        "genres": it.genres,
        "score": round(it.score, 4),
        "confidence": round(it.confidence, 4),
        "explore": it.confidence < 0.5,
        "facet": it.facet,
        "generators": sorted(it.generators),
        "explanation": {k: round(v, 4) for k, v in it.reasons.items()},
        "why": E.describe(it, facts, ctx, pref_summary),
    }
    if it.audio:
        out["audio"] = {k: it.audio[k] for k in ("bpm", "key", "camelot", "energy", "danceability") if k in it.audio}
    if it.source == "catalog":
        out["in_library"] = False
        for k in ("artist", "preview_url", "recording_mbid", "artist_mbid"):
            if it.payload.get(k):
                out[k] = it.payload[k]
    return out


async def _person(pool, user_id: str, item_type: str, *, tz=None, device=None, device_class=None, mood=None, room=None,
                  use_context=True, taste_from=None, blend=None):
    _uuid(user_id)
    async with pool.acquire() as conn:
        canonical, group = await identity_group(conn, user_id)
        facts, overlay = await load_knowledge(pool, canonical)
        prefs = overlay.get("preferences") or {}
        ratio = float(prefs.get("taste_blend", 0.0) or 0.0) if blend is None else blend
        ctx = await effective_context(conn, qdrant(), canonical, tz=tz or prefs.get("tz"), device=device, device_class=device_class,
                                      mood=mood, room=room, use_signals=use_context)
        domain_facts = [f for f in facts if f["domain"] in ("general", item_type)]
        p = await E.load_person(conn, qdrant(), canonical, group, item_type, ctx, facts=domain_facts, overlay=overlay,
                                taste_from=taste_from, blend_ratio=ratio)
    return p, ctx


@app.get("/recommendations/{user_id}")
async def recommendations(
    user_id: str,
    item_type: str = Query(..., description=f"one of {ITEM_TYPES}"),
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0.0, le=1.0, description="overrides the explore-slot ratio (default: mood, then the person's overlay, else 0.125)"),
    source: list[str] | None = Query(None, description="only items from these sources, e.g. source=mono (playable) or source=catalog (discovery)"),
    taste_from: str | None = Query(None, description=f"rank with another domain's taste, one of {TASTE_BUCKETS}"),
    blend: float | None = Query(None, ge=0.0, le=1.0, description="share of cross-domain taste mixed in (default: overlay taste_blend)"),
    mood: str | None = Query(None, description="a preset (focus, chill, workout, party, sleep, sad, happy, ...) or free text"),
    tz: str | None = Query(None, description="IANA time zone of the person, for time-of-day context"),
    device: str | None = Query(None, description="device name, e.g. 'Living room TV' (context)"),
    device_class: str | None = Query(None, description="phone | desktop | tv | car | speaker | tablet"),
    room: str | None = Query(None, description="room for room-level context (Home Assistant)"),
    use_context: bool = Query(True, description="apply stored context signals (mood, activity, weather, calendar)"),
    surface: str = Query("recommendations", description="where this list is shown, for impression logging"),
    log: bool = Query(True, description="log impressions (what was shown), which is how Muse learns from lists"),
) -> dict:
    if item_type not in ITEM_TYPES:
        raise HTTPException(status_code=400, detail=f"item_type must be one of {ITEM_TYPES}")
    if taste_from is not None and taste_from not in TASTE_BUCKETS:
        raise HTTPException(status_code=400, detail=f"taste_from must be one of {TASTE_BUCKETS}")
    pool = await get_pool()
    p, ctx = await _person(pool, user_id, item_type, tz=tz, device=device, device_class=device_class, mood=mood, room=room,
                           use_context=use_context, taste_from=taste_from, blend=blend)
    if explore_ratio is None:
        explore_ratio = float(ctx.mood_def.get("explore")) if ctx.mood_def.get("explore") is not None else \
            float(p.overlay.get("explore_ratio") or EXPLORE_RATIO_DEFAULT)
    base = {"user_id": user_id, "canonical_user_id": str(p.canonical), "item_type": item_type, "context": ctx.summary(), "taste": p.taste_how}
    async with pool.acquire() as conn:
        found = await E.generate(conn, qdrant(), p, ctx, sources=source, exclude_sources=p.overlay.get("disabled_sources") or None)
        if not found:
            return {**base, "recommendations": [], "note": "cold-start: no taste vector yet"}
        items = await E.featurize(conn, qdrant(), p, ctx, found)
        weights, ranker = await weights_for(conn, item_type)
    items = apply_knowledge(E.score(items, weights), p.facts, p.overlay.get("source_weights") or {})
    E.penalize_ignored(items, p.ignored)
    final = E.rerank(items, limit, explore_ratio, p.facets)
    request_id = uuid.uuid4()
    if log:
        await log_impressions(pool, request_id, p.canonical, item_type, final, surface)
    return {**base, "request_id": str(request_id), "ranker": ranker,
            "recommendations": [_item_json(it, p.facts, ctx, p.pref_summary) for it in final]}


async def _track(conn, key: str) -> S.Track:
    """The current song as a sequencing Track (it's usually already heard, so not a candidate)."""
    from muse_schema import content_point_id

    s_, sid = key.split(":", 1)
    pid = content_point_id(s_, sid)
    tv = E._point(qdrant(), "content_items", pid)
    av = E._point(qdrant(), "audio_items", pid)
    bv = E._point(qdrant(), "behavior_items", pid)
    feats = (await E.audio_features(conn, [key])).get(key, {})
    title = ""
    try:
        pts = qdrant().retrieve("content_items", [pid], with_payload=True)
        title = (pts[0].payload or {}).get("title", "") if pts else ""
    except Exception:
        pass
    return S.Track(key, title, title.split(" - ")[0] if " - " in title else "", feats, S._unit(tv), S._unit(av), S._unit(bv), 0.5)


async def _sequence_pool(pool, p: E.Person, ctx, seed_keys: list[str], history: list[str], sources, discovery: float):
    """Candidates for what comes next: familiar favourites and new things, minus what just played."""
    async with pool.acquire() as conn:
        recent = {f"{r['source']}:{r['source_item_id']}" for r in await conn.fetch(
            "SELECT DISTINCT source, source_item_id FROM event WHERE user_id = ANY($1::uuid[]) AND source_item_id IS NOT NULL "
            "AND action IN ('played', 'completed', 'skipped') AND \"timestamp\" > now() - interval '3 hours'", p.group)}
        loved = {f"{r['source']}:{r['source_item_id']}": float(r["w"]) for r in await conn.fetch(
            "SELECT source, source_item_id, sum(action_weight) AS w FROM event WHERE user_id = ANY($1::uuid[]) AND item_type = $2 "
            "AND source_item_id IS NOT NULL GROUP BY 1, 2", p.group, p.item_type)}
        skipped_lately = {f"{r['source']}:{r['source_item_id']}" for r in await conn.fetch(
            "SELECT DISTINCT source, source_item_id FROM event WHERE user_id = ANY($1::uuid[]) AND action = 'skipped' "
            "AND \"timestamp\" > now() - interval '14 days'", p.group)}
        p.owned = recent | set(history) | set(seed_keys) | skipped_lately  # radio may replay favourites, not what just played
        p.exclude_heard = False
        seeds = [tuple(k.split(":", 1)) for k in seed_keys]
        found = await E.generate(conn, qdrant(), p, ctx, sources=sources, seeds=seeds)
        for k, w in loved.items():  # favourites are radio material too
            if w > 1.0 and k not in p.owned and (not sources or k.split(":", 1)[0] in sources):
                found.setdefault(k, set()).add("favourite")
        items = await E.featurize(conn, qdrant(), p, ctx, found)
        weights, _ = await weights_for(conn, p.item_type)
        keys = [it.key for it in items] + seed_keys
        after_rows = await conn.fetch(
            "SELECT from_key, to_key, (count + 2 * completions - 2 * skips) AS s FROM item_transition "
            "WHERE from_key = ANY($1::text[]) AND to_key = ANY($1::text[])", keys) if keys else []
    items = apply_knowledge(E.score(items, weights), p.facts, p.overlay.get("source_weights") or {})
    top = max((it.score for it in items), default=1.0) or 1.0
    tracks = []
    for it in items:
        familiar = loved.get(it.key, 0.0)
        bonus = (1 - discovery) * min(familiar, 3.0) / 3.0 * 0.3 if familiar > 0 else discovery * 0.15
        tracks.append(S.from_item(it, max(0.0, min(1.0, it.score / top + bonus))))
    m = max((float(r["s"]) for r in after_rows), default=0.0)
    after = {(r["from_key"], r["to_key"]): max(0.0, float(r["s"])) / m for r in after_rows} if m > 0 else {}
    return tracks, after


def _seq_json(t: S.Track, s: float, parts: dict, prev: S.Track, facts, ctx) -> dict:
    out = _item_json(t.item, facts, ctx) if t.item is not None else {"source": t.key.split(":", 1)[0], "source_item_id": t.key.split(":", 1)[1]}
    out["transition"] = {"score": round(s, 4), **{k: round(v, 3) for k, v in parts.items()}}
    out["why"] = S.explain(prev, t, parts) + [w for w in out.get("why", []) if w not in S.explain(prev, t, parts)][:1]
    return out


@app.get("/next/{user_id}")
async def next_item(
    user_id: str,
    source: str = Query(..., description="source of what is playing now"),
    source_item_id: str = Query(...),
    item_type: str = "track",
    n: int = Query(5, ge=1, le=50),
    history: list[str] | None = Query(None, description="'source:id' of the session so far (not repeated)"),
    arc: str = Query("steady", description=f"energy curve: {S.ARCS}"),
    discovery: float = Query(0.3, ge=0, le=1, description="0 = favourites, 1 = only new to you"),
    playable_source: list[str] | None = Query(None, description="restrict to sources the player can play, e.g. mono"),
    mood: str | None = None, tz: str | None = None, device: str | None = None, room: str | None = None,
) -> dict:
    if arc not in S.ARCS:
        raise HTTPException(400, f"arc must be one of {S.ARCS}")
    pool = await get_pool()
    p, ctx = await _person(pool, user_id, item_type, tz=tz, device=device, mood=mood, room=room)
    key = f"{source}:{source_item_id}"
    async with pool.acquire() as conn:
        cur = await _track(conn, key)
    pool_tracks, after = await _sequence_pool(pool, p, ctx, [key], history or [], playable_source, discovery)
    hist_artists = [t for t in (history or [])]
    picks = S.next_tracks(cur, pool_tracks, n, after, arc=arc, history_artists=hist_artists)
    return {"user_id": user_id, "after": {"key": key, "title": cur.title, "audio": {k: cur.audio.get(k) for k in ("bpm", "camelot", "energy") if k in cur.audio}},
            "context": ctx.summary(), "next": [_seq_json(t, s, parts, cur, p.facts, ctx) for t, s, parts in picks]}


@app.get("/radio/{user_id}")
async def radio(
    user_id: str,
    seed_source: str | None = Query(None, description="start after this item (else from the person's taste)"),
    seed_id: str | None = None,
    item_type: str = "track",
    length: int = Query(25, ge=2, le=100),
    arc: str = Query("steady", description=f"energy curve: {S.ARCS}"),
    discovery: float = Query(0.3, ge=0, le=1),
    playable_source: list[str] | None = Query(None),
    mood: str | None = None, tz: str | None = None, device: str | None = None, room: str | None = None,
) -> dict:
    if arc not in S.ARCS:
        raise HTTPException(400, f"arc must be one of {S.ARCS}")
    pool = await get_pool()
    p, ctx = await _person(pool, user_id, item_type, tz=tz, device=device, mood=mood, room=room)
    seed_keys = [f"{seed_source}:{seed_id}"] if seed_source and seed_id else []
    pool_tracks, after = await _sequence_pool(pool, p, ctx, seed_keys, [], playable_source, discovery)
    if not pool_tracks:
        return {"user_id": user_id, "tracks": [], "note": "nothing to play yet"}
    if seed_keys:
        async with pool.acquire() as conn:
            seed = await _track(conn, seed_keys[0])
    else:
        seed = max(pool_tracks, key=lambda t: t.taste)
    seq = S.radio(seed, pool_tracks, length if seed_keys else length - 1, after, arc=arc)
    out, prev = [], seed
    if not seed_keys:
        out.append(_seq_json(seed, seed.taste, {"taste": seed.taste}, seed, p.facts, ctx))
    for t, s, parts in seq:
        out.append(_seq_json(t, s, parts, prev, p.facts, ctx))
        prev = t
    request_id = uuid.uuid4()
    await log_impressions(pool, request_id, p.canonical, item_type, [t.item for t, _, _ in seq if t.item is not None], "radio")
    return {"user_id": user_id, "request_id": str(request_id), "seed": seed.key, "arc": arc, "context": ctx.summary(), "tracks": out}


async def _ctx_for(mood=None, tz=None, room=None):
    async def f(conn, canonical):
        return await effective_context(conn, qdrant(), canonical, tz=tz, mood=mood, room=room)
    return f


@app.get("/group-recommendations")
async def group_recommendations(
    user_id: list[str] | None = Query(None, description="the people watching/listening together"),
    session: str | None = Query(None, description="or a group session id (members from the session)"),
    item_type: str = Query("movie"),
    limit: int = Query(20, ge=1, le=100),
    seen: str = Query("all", description="drop items seen by: all (default) | any | none of the members"),
    source: list[str] | None = Query(None),
    mood: str | None = None, tz: str | None = None, room: str | None = None,
) -> dict:
    if item_type not in ITEM_TYPES:
        raise HTTPException(400, f"item_type must be one of {ITEM_TYPES}")
    pool = await get_pool()
    members = list(user_id or [])
    if session:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT members, room FROM group_session WHERE id = $1 AND ended_at IS NULL", _uuid(session))
        if row is None:
            raise HTTPException(404, "no active group session with that id")
        members += [str(m) for m in row["members"]]
        room = room or row["room"]
    members = list(dict.fromkeys(members))
    if len(members) < 1:
        raise HTTPException(400, "pass user_id at least once, or a session")
    for m in members:
        _uuid(m)
    res = await G.group_recommend(pool, qdrant(), members, item_type, limit, await _ctx_for(mood, tz, room), seen=seen,
                                  sources=source, load_knowledge=load_knowledge)
    return {
        "members": res["members"], "fairness": res["fairness"], "item_type": item_type, "vetoed": res["vetoed"],
        "recommendations": [{**_item_json(it, [], None), "member_fit": it.member_fits, "seen_by": it.seen_by} for it in res["items"]],
    }


@app.get("/fit/{source}/{source_item_id}")
async def fit(source: str, source_item_id: str, user_id: list[str] = Query(...), item_type: str = "movie") -> dict:
    for m in user_id:
        _uuid(m)
    pool = await get_pool()
    return {"fits": await G.member_fits(pool, qdrant(), user_id, source, source_item_id, item_type, await _ctx_for(), load_knowledge)}


@app.get("/explain/{user_id}/{source}/{source_item_id}")
async def explain(user_id: str, source: str, source_item_id: str, item_type: str = "track", limit: int = 20) -> dict:
    """Why an item is (not) recommended to someone: blockers, rank, and its score breakdown."""
    pool = await get_pool()
    p, ctx = await _person(pool, user_id, item_type)
    key = f"{source}:{source_item_id}"
    blockers = []
    if key in p.owned:
        blockers.append("already played or judged: recommendations only show things new to you")
    if source in (p.overlay.get("disabled_sources") or []):
        blockers.append(f"the source '{source}' is turned off in your settings")
    if p.ignored.get(key):
        blockers.append(f"shown {p.ignored[key]} times recently without a reaction, so it is ranked lower")
    owned = set(p.owned)
    p.owned, p.exclude_heard = set(), False
    async with pool.acquire() as conn:
        found = await E.generate(conn, qdrant(), p, ctx)
        reached = set(found.get(key, set()))
        found.setdefault(key, set()).add("asked")
        items = await E.featurize(conn, qdrant(), p, ctx, found)
        weights, _ = await weights_for(conn, item_type)
    scored = E.score(items, weights)
    kept = apply_knowledge(list(scored), p.facts, p.overlay.get("source_weights") or {})
    target = next((it for it in scored if it.key == key), None)
    if target is None:
        return {"item": key, "recommendable": False, "blockers": blockers + ["not embedded yet, or not a " + item_type]}
    if target not in kept:
        stated = [f["statement"] for f in p.facts if f["kind"] == "avoid"]
        blockers.append("filtered by something you told Muse" + (f": {stated[0]}" if stated else ""))
    E.penalize_ignored(kept, p.ignored)
    ranked = sorted([it for it in kept if it.key not in owned or it.key == key], key=lambda c: -c.score)
    rank = next((i + 1 for i, it in enumerate(ranked) if it.key == key), None)
    cutoff = ranked[limit - 1].score if len(ranked) >= limit else None
    if rank and rank > limit:
        strongest = sorted(target.reasons.items(), key=lambda kv: kv[1])[:2]
        blockers.append(f"outranked: #{rank} of {len(ranked)} (a list shows {limit}); weakest terms: " + ", ".join(k for k, _ in strongest))
    return {"item": key, "title": target.title, "recommendable": not blockers, "rank": rank, "candidates": len(ranked),
            "cutoff_score": round(cutoff, 4) if cutoff is not None else None, "blockers": blockers,
            "found_by": sorted(reached) or ["none of the generators reached it on its own"],
            "detail": _item_json(target, p.facts, ctx, p.pref_summary)}


@app.get("/context/{user_id}")
async def context(user_id: str, tz: str | None = None, device: str | None = None, room: str | None = None) -> dict:
    """The context Muse would apply right now (time, device, mood and where it came from)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        canonical, _ = await identity_group(conn, _uuid(user_id) and user_id)
        ctx = await effective_context(conn, qdrant(), canonical, tz=tz, device=device, room=room)
    return {"user_id": user_id, "canonical_user_id": str(canonical), **ctx.summary()}


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
