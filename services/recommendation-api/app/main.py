"""Muse Recommendation API.

The 3-stage funnel (candidates -> ranking -> re-ranking, docs/architecture.md) now lives in
funnel.py and powers every row type, single people and groups. Around it: "Why this"
explanations, search by meaning, the user-control overlay (explore dial, moods), linked
identities across apps, predictive prefetch, the yearly Wrapped, Soon Gone and stats.
Integration guide for clients: docs/vesper.md.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

import os

from . import embed, funnel, identity, moods, overlay, prefetch as prefetch_mod, search as search_mod, wrapped as wrapped_mod
from . import soon_gone as soon_gone_repo
from .auth import require_key
from .db import close_pool, get_pool
from .migrate import migrate

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.recommendation")

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")
ITEM_TYPES = ["movie", "series", "series_episode", "youtube_video", "track", "audiobook", "ebook"]

app = FastAPI(title="Muse Recommendation API", version="0.3.0", dependencies=[Depends(require_key)])
_qdrant: QdrantClient | None = None


def qdrant() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL)
    return _qdrant


@app.on_event("startup")
async def _startup() -> None:
    try:
        applied = await migrate(await get_pool())
        if applied:
            logger.info("migrations applied: %s", ", ".join(applied))
    except Exception:  # noqa: BLE001 - serve anyway; endpoints that need new tables will say so
        logger.exception("migrations failed")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- helpers -------------------------------------------------------------------------


def _uuid(v: str, what: str = "user_id") -> str:
    try:
        return str(uuid.UUID(v))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{what} must be a UUID")


def _types(item_type: str) -> list[str]:
    types = [t.strip() for t in item_type.split(",") if t.strip()]
    bad = [t for t in types if t not in ITEM_TYPES]
    if not types or bad:
        raise HTTPException(status_code=400, detail=f"item_type must be one or more of {ITEM_TYPES}")
    return types


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


async def _users(primary: str, with_users: str | None) -> list[str]:
    ids = [_uuid(primary)] + [_uuid(u) for u in _csv(with_users)]
    return await identity.canonical_many(await get_pool(), ids)


# --- recommendations -------------------------------------------------------------------


async def _recommend(users: list[str], item_type: str, row: str, seed: str | None, seed_source: str, seed_rank: int, limit: int,
                     explore_ratio: float | None, max_age: int | None, allow_unrated: bool, exclude: str | None,
                     mood: str | None, include_seen: bool) -> dict:
    if row == "picks" and len(users) > 1:
        row = "group"
    if mood is not None and mood != "" and mood not in moods.MOODS:
        raise HTTPException(status_code=400, detail=f"mood must be one of {sorted(moods.MOODS)}")
    params = funnel.Params(
        user_ids=users, item_types=_types(item_type), row=row, seed=seed, seed_source=seed_source, seed_rank=seed_rank,
        limit=limit, explore_ratio=explore_ratio, max_age=max_age, allow_unrated=allow_unrated,
        exclude=set(_csv(exclude)), mood=mood, include_seen=include_seen,
    )
    try:
        return await funnel.recommend(await get_pool(), qdrant(), params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/recommendations/group")
async def recommendations_group(
    user_ids: str = Query(..., description="comma-separated user_ids watching together; the first is the one holding the remote"),
    item_type: str = Query("movie,series"),
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0.0, le=1.0),
    max_age: int | None = Query(None, ge=0, le=21),
    allow_unrated: bool = False,
    exclude: str | None = None,
    mood: str | None = None,
) -> dict:
    ids = _csv(user_ids)
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="a group needs at least two user_ids")
    users = await identity.canonical_many(await get_pool(), [_uuid(u) for u in ids])
    return await _recommend(users, item_type, "group", None, "jellyfin", 0, limit, explore_ratio, max_age, allow_unrated, exclude, mood, False)


@app.get("/recommendations/{user_id}")
async def recommendations(
    user_id: str,
    item_type: str = Query(..., description=f"one or more (comma-separated) of {ITEM_TYPES}"),
    row: str = Query("picks", description=f"one of {funnel.ROWS}"),
    seed: str | None = Query(None, description="source_item_id the row is seeded with (more_like_this; optional for because_you_watched)"),
    seed_source: str = "jellyfin",
    seed_rank: int = Query(0, ge=0, le=5, description="because_you_watched without a seed: the n-th anchor Muse picks"),
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0.0, le=1.0, description="overrides the overlay's explore dial"),
    max_age: int | None = Query(None, ge=0, le=21, description="profile ceiling: only titles rated for this age or younger"),
    allow_unrated: bool = False,
    exclude: str | None = Query(None, description="comma-separated source_item_ids to leave out (already on screen)"),
    mood: str | None = Query(None, description="overrides the overlay mood; empty string = no mood"),
    include_seen: bool = False,
    with_users: str | None = Query(None, description="comma-separated co-viewers: makes this a group row"),
) -> dict:
    users = await _users(user_id, with_users)
    return await _recommend(users, item_type, row, seed, seed_source, seed_rank, limit, explore_ratio, max_age, allow_unrated, exclude, mood, include_seen)


@app.get("/explain/{user_id}/{source_item_id}")
async def explain(user_id: str, source_item_id: str, source: str = "jellyfin", with_users: str | None = None) -> dict:
    """"Why this" for any title: a match percentage and reason codes (reasons.py)."""
    users = await _users(user_id, with_users)
    return await funnel.explain(await get_pool(), qdrant(), users, source, source_item_id)


class SemanticSearch(BaseModel):
    query: str = Field(..., min_length=2, max_length=300)
    user_id: str | None = None
    item_types: list[str] = Field(default_factory=lambda: ["movie", "series"])
    limit: int = Field(20, ge=1, le=60)
    max_age: int | None = Field(None, ge=0, le=21)
    allow_unrated: bool = False


@app.post("/search/semantic")
async def search_semantic(req: SemanticSearch) -> dict:
    vec = await embed.embed_one(req.query.strip())
    if vec is None:
        raise HTTPException(status_code=503, detail="embedding model unavailable")
    profile = None
    if req.user_id:
        uid = await identity.canonical(await get_pool(), _uuid(req.user_id))
        profile = await funnel.build_profile(await get_pool(), qdrant(), uid, req.item_types)
    hits = await search_mod.semantic(qdrant(), vec, _types(",".join(req.item_types)), req.limit, profile, req.max_age, req.allow_unrated)
    return {"query": req.query, "results": hits}


# --- overlay & moods -----------------------------------------------------------------------


@app.get("/moods")
async def list_moods() -> dict:
    return {"moods": [{"id": m.id, "max_runtime_minutes": m.max_runtime_minutes, "rewatch": m.rewatch} for m in moods.MOODS.values()],
            "default_hours": moods.DEFAULT_MOOD_HOURS}


class OverlayUpdate(BaseModel):
    explore_ratio: float | None = Field(None, ge=0.0, le=1.0)
    mood: str | None = None
    mood_hours: float | None = Field(None, gt=0, le=48)
    clear_mood: bool = False


@app.get("/users/{user_id}/overlay")
async def get_overlay(user_id: str) -> dict:
    uid = await identity.canonical(await get_pool(), _uuid(user_id))
    return {"user_id": uid, **await overlay.get(await get_pool(), uid)}


@app.put("/users/{user_id}/overlay")
async def put_overlay(user_id: str, body: OverlayUpdate) -> dict:
    uid = await identity.canonical(await get_pool(), _uuid(user_id))
    try:
        ov = await overlay.put(await get_pool(), uid, body.explore_ratio, body.mood, body.mood_hours, body.clear_mood)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"user_id": uid, **ov}


@app.delete("/users/{user_id}")
async def delete_user(user_id: str) -> dict:
    """Per-user wipe, e.g. when a Vesper profile is deleted."""
    return await identity.delete_user(await get_pool(), qdrant(), _uuid(user_id))


# --- linked identities ---------------------------------------------------------------------


class LinkCodeRequest(BaseModel):
    source: str | None = Field(None, max_length=40)
    label: str | None = Field(None, max_length=80)


class LinkRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=12)
    user_id: str
    source: str | None = Field(None, max_length=40)
    label: str | None = Field(None, max_length=80)


@app.post("/identities/{user_id}/link-code")
async def create_link_code(user_id: str, body: LinkCodeRequest) -> dict:
    return await identity.create_code(await get_pool(), _uuid(user_id), body.source, body.label)


@app.post("/identities/link")
async def redeem_link_code(body: LinkRequest) -> dict:
    try:
        return await identity.redeem(await get_pool(), qdrant(), body.code, _uuid(body.user_id), body.source, body.label)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/identities/{user_id}")
async def get_identity(user_id: str) -> dict:
    return await identity.links(await get_pool(), _uuid(user_id))


@app.delete("/identities/{user_id}/links/{alias_user_id}")
async def remove_link(user_id: str, alias_user_id: str) -> dict:
    try:
        return await identity.unlink(await get_pool(), qdrant(), _uuid(user_id), _uuid(alias_user_id, "alias_user_id"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


class Watching(BaseModel):
    user_ids: list[str] = Field(..., min_length=1, max_length=8)


@app.post("/watching")
async def watching(body: Watching) -> dict:
    """A client says who is watching together right now. Muse remembers the group (group
    rows lean on what it watched together) and returns the canonical member ids."""
    members = await identity.note_group(await get_pool(), [_uuid(u) for u in body.user_ids])
    return {"members": members}


# --- prefetch, wrapped -------------------------------------------------------------------


@app.get("/prefetch/{user_id}")
async def prefetch(
    user_id: str,
    horizon_hours: int = Query(24, ge=1, le=168),
    tz: str | None = Query(None, description="IANA time zone of the household, e.g. Europe/Amsterdam"),
    limit: int = Query(10, ge=1, le=50),
    max_age: int | None = Query(None, ge=0, le=21),
) -> dict:
    uid = await identity.canonical(await get_pool(), _uuid(user_id))
    zone = prefetch_mod.tzinfo(tz)
    now = prefetch_mod.now_utc()
    acts, rows = await prefetch_mod.load(await get_pool(), uid)
    p_active = prefetch_mod.active_probability(prefetch_mod.slot_rates(acts, zone, now), now, horizon_hours, zone)
    items = prefetch_mod.rank_items(rows, now)
    if len(items) < limit:
        try:
            recs = await funnel.recommend(await get_pool(), qdrant(), funnel.Params(user_ids=[uid], item_types=["movie", "series"], limit=limit, max_age=max_age, explore_ratio=0.0))
            for r in recs["recommendations"][:3]:
                items.append({"source": r["source"], "source_item_id": r["source_item_id"], "item_type": r["item_type"],
                              "title": r["title"], "reason": "recommended", "base": 0.25 * (r["match"] or 50) / 100})
        except Exception:  # noqa: BLE001 - prefetch must not fail on a cold profile
            pass
    for it in items:
        it["likelihood"] = round(it.pop("base") * max(p_active, 0.15), 4)
    items.sort(key=lambda x: -x["likelihood"])
    return {"user_id": uid, "horizon_hours": horizon_hours, "active_probability": round(p_active, 4), "items": items[:limit]}


@app.get("/wrapped/{user_id}")
async def wrapped(user_id: str, year: int | None = Query(None, ge=2000, le=2100), tz: str | None = None, kids: bool = False) -> dict:
    uid = await identity.canonical(await get_pool(), _uuid(user_id))
    y = year or datetime.now(timezone.utc).year
    zone = prefetch_mod.tzinfo(tz)
    rows = await wrapped_mod.load(await get_pool(), uid, y, zone)
    return {"user_id": uid, "year": y, **wrapped_mod.aggregate(rows, zone, kids)}


# --- stats, soon gone ----------------------------------------------------------------------


@app.get("/stats")
async def stats() -> dict:
    """For admin consoles: is Muse learning? Event volume, users, embedding progress."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT count(*) AS events,
                   count(*) FILTER (WHERE ingested_at > now() - interval '24 hours') AS events_24h,
                   count(DISTINCT user_id) AS users,
                   max(ingested_at) AS last_event_at
            FROM event
            """
        )
        by_source = await conn.fetch(
            "SELECT source, count(*) AS n, max(ingested_at) AS last FROM event WHERE ingested_at > now() - interval '7 days' GROUP BY source ORDER BY n DESC"
        )
        by_action = await conn.fetch(
            "SELECT action, count(*) AS n FROM event WHERE ingested_at > now() - interval '7 days' GROUP BY action ORDER BY n DESC"
        )
        watermark = await conn.fetchval("SELECT last_processed_at FROM embedding_worker_state")
        pending_embed = await conn.fetchval("SELECT count(*) FROM event WHERE ingested_at > $1", watermark) if watermark else None
        links = await conn.fetchval("SELECT count(*) FROM identity_link")
        soon = await conn.fetchval("SELECT count(*) FROM soon_gone WHERE whitelisted = FALSE AND removed = FALSE")
    counts = {}
    for name in ("content_items", "taste_vectors"):
        try:
            counts[name] = qdrant().count(collection_name=name, exact=False).count
        except Exception:  # noqa: BLE001
            counts[name] = None
    iso = lambda v: v.isoformat() if v else None  # noqa: E731
    return {
        "events": row["events"], "events_24h": row["events_24h"], "users": row["users"], "last_event_at": iso(row["last_event_at"]),
        "sources_7d": [{"source": r["source"], "events": r["n"], "last_event_at": iso(r["last"])} for r in by_source],
        "actions_7d": {r["action"]: r["n"] for r in by_action},
        "embedding": {"watermark": iso(watermark), "pending_events": pending_embed, "model_reachable": await embed.healthy(), **counts},
        "linked_identities": links, "soon_gone_pending": soon,
    }


@app.get("/soon-gone")
async def soon_gone_list() -> dict:
    """Backs the "Soon Gone" row (Jellyfin plugin, Vesper). Each item has its Jellyfin id."""
    return {"items": await soon_gone_repo.list_pending(await get_pool())}


@app.post("/soon-gone/{item_id}/whitelist")
async def soon_gone_whitelist(item_id: str, user_id: str = Query(...)) -> dict:
    """Keep it: called on playback start and from Keep buttons. Accepts the curator's
    tmdb/tvdb id or a Jellyfin item id; a no-op when nothing is pending."""
    uid = await identity.canonical(await get_pool(), _uuid(user_id))
    whitelisted = await soon_gone_repo.whitelist(await get_pool(), item_id, uid)
    return {"source_item_id": item_id, "whitelisted": whitelisted}
