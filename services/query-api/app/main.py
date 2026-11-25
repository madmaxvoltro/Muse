"""Muse Query API — read *and* teach Muse, for people's apps and their assistants.

Everything an assistant needs to know someone's taste and help shape it:

* search across all sources (semantic + keyword, personalised), item details, similar items
* profile summaries, history, recommendations (incl. cross-domain: music taste -> films)
* facts ("remember that...") that feed the taste vector, explicit ratings
* questions Muse wants answered, and answering them
* identity: link codes to join accounts from different services into one person
* tokens for MCP connections, feature suggestions for Muse itself

The MCP server (services/mcp-server) is a thin client of this API. Auth: see app/auth.py.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from muse_schema import RATED_WEIGHTS, identity_group
from muse_schema.storage import insert_event
from muse_schema.vectors import SealedQdrant, qdrant_client
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

from . import groups, guards, identity, knowledge, mine, music, profile, questions, recclient, search, situation
from .auth import Principal, admin_only, current_principal, resolve_user, writer
from .db import close_pool, get_pool
from .embedder import get_embedder
from .items import DOMAINS, ITEM_TYPES, interactions, item_details, summarize_metadata

QDRANT_URL = os.environ.get("MUSE_QDRANT_URL", "http://qdrant:6333")

app = FastAPI(title="Muse Query API", version="0.2.0")
app.include_router(music.router)
app.include_router(situation.router)
app.include_router(groups.router)
app.include_router(mine.router)


@app.middleware("http")
async def access_log(request, call_next):
    """Who touched whose data: every authenticated /v1 call is logged (GET /v1/users/{u}/access-log)."""
    response = await call_next(request)
    p = getattr(request.state, "principal", None)
    if p is not None and request.url.path.startswith("/v1/"):
        try:
            parts = request.url.path.split("/")
            subject = None
            if len(parts) > 3 and parts[2] == "users" and parts[3] not in ("", "me"):
                try:
                    subject = uuid.UUID(parts[3])
                except ValueError:
                    subject = None
            if subject is None and p.user_id is not None:
                subject = p.user_id
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO access_log (principal, token_id, actor_user_id, subject_user_id, method, path, status) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                    p.kind if p.kind != "adapter" else f"adapter:{p.source}", p.token_id, p.user_id if p.kind == "user" else None, subject,
                    request.method, request.url.path[:300], response.status_code)
        except Exception:
            pass  # logging must never break a request
    return response
_qdrant: QdrantClient | None = None


def qdrant() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = qdrant_client(QDRANT_URL)  # API key + sealed payloads (muse_schema.vectors)
    return _qdrant


def set_qdrant(client: QdrantClient) -> None:
    global _qdrant
    _qdrant = client if isinstance(client, SealedQdrant) else SealedQdrant(client)


@app.on_event("startup")
async def _startup() -> None:
    import asyncio

    from .vectors import ensure_mood_vectors

    async def moods_later():
        try:
            await ensure_mood_vectors(qdrant())
        except Exception:  # Qdrant or the embedders not up yet: the next start retries
            pass

    if os.environ.get("MUSE_QUERY_EMBED_MOODS", "1") == "1":
        asyncio.get_running_loop().create_task(moods_later())


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _uuid(v: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except ValueError:
        raise HTTPException(400, f"{what} must be a UUID")


def _via(p: Principal) -> str:
    return p.source or p.kind


# --- who --------------------------------------------------------------------------------


@app.get("/v1/me")
async def me(p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if p.admin:
            return {"kind": "admin", "label": p.label, "can_write": p.can_write,
                    "note": "acts for the whole household; pass user=<id> (see /v1/users)",
                    "users": await identity.list_people(conn)}
        uid = await resolve_user(conn, p, "me")
        return {"kind": p.kind, "label": p.label, "can_write": p.can_write, **await identity.describe_identity(conn, uid)}


@app.get("/v1/users")
async def users(p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if p.admin:
            return {"users": await identity.list_people(conn)}
        uid = await resolve_user(conn, p, "me")
        return {"users": [await identity.describe_identity(conn, uid)]}


@app.get("/v1/users/{user}/identity")
async def user_identity(user: str, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await identity.describe_identity(conn, await resolve_user(conn, p, user))


class UserPatch(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=80)


@app.put("/v1/users/{user}")
async def rename(user: str, body: UserPatch, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        await conn.execute("INSERT INTO muse_user (user_id, display_name) VALUES ($1, $2) "
                           "ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name", uid, body.display_name)
        return await identity.describe_identity(conn, uid)


# --- identity linking -----------------------------------------------------------------------


@app.post("/v1/identity/link-codes")
async def link_code(user: str = "me", p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        await identity.ensure_user(conn, uid)
        return await identity.create_link_code(conn, uid)


class LinkIn(BaseModel):
    code: str
    label: str | None = None  # how this identity shows up, e.g. "Mono: max"


@app.post("/v1/identity/link")
async def link(body: LinkIn, user: str = "me", p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = p.user_id if (user == "me" and p.kind == "adapter") else await resolve_user(conn, p, user)
        label = body.label or (f"{p.source}" if p.source else None)
        caller = f"{p.kind}:{p.source or ''}:{uid}"
        guards.link_failures.check(caller)
        try:
            result = await identity.redeem_link_code(conn, uid, body.code, label)
        except HTTPException:
            guards.link_failures.hit(caller)
            raise
        return {**result, "identity": await identity.describe_identity(conn, uuid.UUID(result["user_id"]))}


class AliasIn(BaseModel):
    user_id: str  # canonical person
    alias_id: str  # id to fold into them
    label: str | None = None


@app.post("/v1/identity/aliases")
async def admin_link(body: AliasIn, p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await identity.merge(conn, _uuid(body.user_id), _uuid(body.alias_id), "admin", body.label)


@app.delete("/v1/identity/aliases/{alias_id}")
async def unlink(alias_id: str, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        aid = _uuid(alias_id)
        if not p.admin:
            mine = await resolve_user(conn, p, "me")
            owner = await conn.fetchval("SELECT user_id FROM user_alias WHERE alias_id = $1", aid)
            if owner != mine:
                raise HTTPException(403, "not one of your linked identities")
        return await identity.unlink(conn, aid)


# --- tokens ---------------------------------------------------------------------------------


class TokenIn(BaseModel):
    label: str | None = None
    can_write: bool = True
    user: str | None = "me"  # admin: a user id, or null for a household token
    display_name: str | None = None  # set the person's name while at it (adapters know it)
    expires_days: int | None = Field(None, ge=1, le=3650, description="token stops working after this many days")


@app.post("/v1/tokens")
async def new_token(body: TokenIn, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if body.user is None:
            if not p.admin:
                raise HTTPException(403, "only an admin can create household tokens")
            return await identity.create_token(conn, None, body.label or "household", body.can_write, body.expires_days)
        uid = await resolve_user(conn, p, body.user)
        if body.display_name:
            await identity.ensure_user(conn, uid, body.display_name)
        return await identity.create_token(conn, uid, body.label, body.can_write, body.expires_days)


@app.get("/v1/tokens")
async def tokens(user: str = "me", p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = None if (p.admin and user == "me") else await resolve_user(conn, p, user)
        return {"tokens": await identity.list_tokens(conn, uid)}


@app.delete("/v1/tokens/{token_id}")
async def revoke(token_id: str, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = None if p.admin else await resolve_user(conn, p, "me")
        row = await conn.fetchrow(
            "UPDATE api_token SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL AND ($2::uuid IS NULL OR user_id = $2) RETURNING id",
            _uuid(token_id), owner,
        )
        if row is None:
            raise HTTPException(404, "no such token")
        return {"revoked": token_id}


# --- read: search, items, profile, history, recommendations -------------------------------------


def _types(item_type: list[str] | None) -> list[str] | None:
    for t in item_type or []:
        if t not in ITEM_TYPES:
            raise HTTPException(400, f"item_type must be one of {ITEM_TYPES}")
    return item_type or None


@app.get("/v1/search")
async def do_search(
    q: str = Query(..., min_length=1, max_length=500),
    item_type: list[str] | None = Query(None),
    source: list[str] | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: str | None = Query("me", description="whose taste/history to apply; admin tokens may pass none"),
    personalize: bool = True,
    include_seen: bool = True,
    max_age: int | None = Query(None, ge=0, le=21, description="kids profiles: only titles rated for this age or younger"),
    allow_unrated: bool = False,
    p: Principal = Depends(current_principal),
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = None if (p.admin and user in (None, "", "me")) else await resolve_user(conn, p, user)
        return await search.search(conn, qdrant(), await get_embedder(), q, item_types=_types(item_type), sources=source,
                                   limit=limit, user_id=uid, personalize=personalize, include_seen=include_seen,
                                   max_age=max_age, allow_unrated=allow_unrated)


@app.get("/v1/items/{source}/{source_item_id}")
async def get_item(source: str, source_item_id: str, user: str | None = "me", p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        key = (source, source_item_id)
        d = (await item_details(conn, [key])).get(key)
        if d is None:
            raise HTTPException(404, "Muse has never seen this item")
        out: dict[str, Any] = {"source": source, "source_item_id": source_item_id, "item_type": d["item_type"],
                               **summarize_metadata(d["item_type"], d["metadata"])}
        if not (p.admin and user in (None, "", "me")):
            uid = await resolve_user(conn, p, user)
            _, group = await identity_group(conn, uid)
            out["you"] = (await interactions(conn, group, [key])).get(key)
        out["similar"] = (await search.similar(conn, qdrant(), source, source_item_id, item_types=[d["item_type"]], limit=5))["results"]
        return out


@app.get("/v1/users/{user}/similar")
async def similar(user: str, source: str, source_item_id: str, item_type: list[str] | None = Query(None),
                  limit: int = Query(10, ge=1, le=50), p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        return await search.similar(conn, qdrant(), source, source_item_id, item_types=_types(item_type), limit=limit, user_id=uid)


@app.get("/v1/users/{user}/profile")
async def get_profile(user: str, domain: str | None = None, days: int = Query(365, ge=1, le=3650),
                      p: Principal = Depends(current_principal)) -> dict:
    if domain is not None and domain not in ITEM_TYPES:
        raise HTTPException(400, f"domain must be one of {ITEM_TYPES}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        return await profile.build_profile(conn, qdrant(), uid, domain, days)


@app.get("/v1/users/{user}/history")
async def get_history(user: str, item_type: str | None = None, action: str | None = None,
                      limit: int = Query(50, ge=1, le=500), before: str | None = None,
                      p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        return {"events": await profile.history(conn, uid, item_type, action, limit, before)}


@app.get("/v1/users/{user}/recommendations")
async def recommend(
    user: str,
    item_type: str,
    limit: int = Query(20, ge=1, le=100),
    explore_ratio: float | None = Query(None, ge=0, le=1),
    taste_from: str | None = None,
    blend: float | None = Query(None, ge=0, le=1),
    source: list[str] | None = Query(None),
    mood: str | None = Query(None, description="a mood preset or a few words"),
    tz: str | None = None,
    p: Principal = Depends(current_principal),
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        try:
            recs = await recclient.recommendations(str(uid), item_type, limit=limit, explore_ratio=explore_ratio,
                                                   taste_from=taste_from, blend=blend, source=source, mood=mood, tz=tz)
        except recclient.RecUnavailable as e:
            raise HTTPException(503, f"recommendation API unavailable: {e}")
        items = recs.get("recommendations", [])
        details = await item_details(conn, [(r["source"], r["source_item_id"]) for r in items])
        for r in items:
            d = details.get((r["source"], r["source_item_id"]))
            if d:
                r.update({k: v for k, v in summarize_metadata(d["item_type"], d["metadata"]).items() if k not in r or not r[k]})
        return recs


@app.get("/v1/users/{user}/radio")
async def radio(user: str, seed_source: str | None = None, seed_id: str | None = None, item_type: str = "track",
                length: int = Query(25, ge=2, le=100), arc: str = "steady", discovery: float = Query(0.3, ge=0, le=1),
                playable_source: list[str] | None = Query(None), mood: str | None = None, tz: str | None = None,
                p: Principal = Depends(current_principal)) -> dict:
    """A session that fits the person and flows (sound, key, tempo, energy arc)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
    try:
        return await recclient.get(f"/radio/{uid}", {"seed_source": seed_source, "seed_id": seed_id, "item_type": item_type, "length": length,
                                                     "arc": arc, "discovery": discovery, "playable_source": playable_source, "mood": mood, "tz": tz})
    except recclient.RecUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/v1/users/{user}/next")
async def next_up(user: str, source: str, source_item_id: str, n: int = Query(5, ge=1, le=50), history: list[str] | None = Query(None),
                  arc: str = "steady", playable_source: list[str] | None = Query(None), mood: str | None = None,
                  p: Principal = Depends(current_principal)) -> dict:
    """What should follow an item for this person."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
    try:
        return await recclient.get(f"/next/{uid}", {"source": source, "source_item_id": source_item_id, "n": n, "history": history,
                                                    "arc": arc, "playable_source": playable_source, "mood": mood})
    except recclient.RecUnavailable as e:
        raise HTTPException(503, str(e))


# --- teach: feedback, facts, settings ------------------------------------------------------------


class FeedbackIn(BaseModel):
    source: str
    source_item_id: str
    rating: int = Field(..., description="-1 thumbs down, 0 clear, 1 thumbs up, 2 love it")
    note: str | None = None


@app.post("/v1/users/{user}/feedback")
async def feedback(user: str, body: FeedbackIn, p: Principal = Depends(writer)) -> dict:
    if body.rating not in RATED_WEIGHTS:
        raise HTTPException(422, f"rating must be one of {sorted(RATED_WEIGHTS)}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        key = (body.source, body.source_item_id)
        d = (await item_details(conn, [key])).get(key)
        if d is None:
            raise HTTPException(404, "Muse has never seen this item; find it with /v1/search first")
        md = {k: v for k, v in d["metadata"].items() if k != "dedupe_key"}
        md.update({"value": body.rating, "via": _via(p)})
        if body.note:
            md["note"] = body.note[:500]
        await insert_event(conn, user_id=uid, source=body.source, source_item_id=body.source_item_id, item_type=d["item_type"],
                           action="rated", action_weight=RATED_WEIGHTS[body.rating], metadata=md)
        return {"rated": body.rating, "title": md.get("title"), "note": "taste vector updates on the embedding worker's next cycle"}


class FactIn(BaseModel):
    statement: str = Field(..., description="what was learned, in plain language")
    kind: str = Field("likes", description="likes | dislikes | avoid | context | fact")
    domain: str = Field("general", description=f"one of {DOMAINS}")
    subject: str | None = Field(None, description="the artist/genre/title it is about, if any")
    sentiment: float | None = Field(None, ge=-1, le=1)
    confidence: float = Field(0.7, ge=0, le=1)


@app.get("/v1/users/{user}/facts")
async def facts(user: str, domain: str | None = None, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return {"facts": await knowledge.list_facts(conn, await resolve_user(conn, p, user), domain)}


@app.post("/v1/users/{user}/facts")
async def add_fact(user: str, body: FactIn, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        source = "user" if p.kind == "adapter" else "assistant"
        return await knowledge.add_fact(conn, uid, body.statement, body.kind, body.domain, body.subject,
                                        body.sentiment, body.confidence, source)


@app.delete("/v1/users/{user}/facts/{fact_id}")
async def retract_fact(user: str, fact_id: str, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await knowledge.retract_fact(conn, await resolve_user(conn, p, user), _uuid(fact_id, "fact_id"))


class SettingsIn(BaseModel):
    explore_ratio: float | None = Field(None, ge=0, le=1)
    taste_blend: float | None = Field(None, ge=0, le=1, description="share of cross-domain taste in every domain")
    source_weights: dict[str, float] | None = None
    disabled_sources: list[str] | None = None


@app.get("/v1/users/{user}/settings")
async def get_settings(user: str, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await knowledge.get_overlay(conn, await resolve_user(conn, p, user))


@app.put("/v1/users/{user}/settings")
async def put_settings(user: str, body: SettingsIn, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        prefs = {"taste_blend": body.taste_blend} if body.taste_blend is not None else None
        return await knowledge.update_overlay(conn, uid, body.explore_ratio, body.source_weights, body.disabled_sources, prefs)


# --- questions --------------------------------------------------------------------------------


@app.get("/v1/users/{user}/questions")
async def get_questions(user: str, limit: int = Query(3, ge=1, le=20), domain: str | None = None,
                        generate: bool = True, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        return {"questions": await questions.open_questions(conn, uid, limit, domain, generate and p.can_write)}


class QuestionIn(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=500)
    kind: str = "free_text"
    domain: str = "general"
    options: list[Any] | None = None
    reason: str | None = None
    priority: float = 0.6


@app.post("/v1/users/{user}/questions")
async def ask_later(user: str, body: QuestionIn, p: Principal = Depends(writer)) -> dict:
    if body.domain not in DOMAINS:
        raise HTTPException(422, f"domain must be one of {DOMAINS}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        return await questions.queue_question(conn, uid, body.prompt, body.kind, body.domain, body.options, body.reason,
                                              _via(p) if p.kind == "adapter" else "assistant", body.priority)


class AnswerIn(BaseModel):
    option: str | None = None
    text: str | None = Field(None, max_length=2000)
    note: str | None = Field(None, max_length=1000, description="anything extra the person said")


async def _question_owner(conn, p: Principal, question_id: uuid.UUID) -> uuid.UUID:
    owner = await conn.fetchval("SELECT user_id FROM question WHERE id = $1", question_id)
    if owner is None:
        raise HTTPException(404, "no such question")
    if not p.admin and owner != await resolve_user(conn, p, "me"):
        raise HTTPException(403, "that question belongs to someone else")
    return owner


@app.post("/v1/questions/{question_id}/answer")
async def answer(question_id: str, body: AnswerIn, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        qid = _uuid(question_id, "question_id")
        owner = await _question_owner(conn, p, qid)
        return await questions.answer(conn, owner, qid, body.option, body.text, body.note, _via(p))


class SkipIn(BaseModel):
    reason: str | None = None


@app.post("/v1/questions/{question_id}/skip")
async def skip(question_id: str, body: SkipIn | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        qid = _uuid(question_id, "question_id")
        owner = await _question_owner(conn, p, qid)
        return await questions.skip(conn, owner, qid, body.reason if body else None)


# --- feature suggestions ------------------------------------------------------------------------


class SuggestionIn(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=3, max_length=4000)
    rationale: str | None = Field(None, max_length=2000)


@app.get("/v1/suggestions")
async def suggestions(status: str | None = None, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return {"suggestions": await knowledge.list_suggestions(conn, status)}


@app.post("/v1/suggestions")
async def suggest(body: SuggestionIn, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = None if p.admin else await resolve_user(conn, p, "me")
        return await knowledge.suggest_feature(conn, uid, body.title, body.description, body.rationale,
                                               _via(p) if p.kind == "adapter" else "assistant")


class SuggestionPatch(BaseModel):
    status: str


@app.put("/v1/suggestions/{suggestion_id}")
async def set_suggestion_status(suggestion_id: str, body: SuggestionPatch, p: Principal = Depends(admin_only)) -> dict:
    if body.status not in ("new", "planned", "done", "declined"):
        raise HTTPException(422, "status must be new, planned, done or declined")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE feature_suggestion SET status = $2 WHERE id = $1 RETURNING *",
                                  _uuid(suggestion_id), body.status)
        if row is None:
            raise HTTPException(404, "no such suggestion")
        return knowledge.suggestion_json(row)


# --- what's connected ----------------------------------------------------------------------------


@app.get("/v1/capabilities")
async def capabilities(p: Principal = Depends(current_principal)) -> dict:
    """Which sources/domains have data — lets an assistant see gaps worth suggesting."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT source, item_type, count(*) AS n, max(\"timestamp\") AS last FROM event GROUP BY 1, 2")
    known = ["jellyfin", "arr", "freetube", "searxng", "openwebui", "navidrome", "audiobookshelf", "mono"]
    present = sorted({r["source"] for r in rows})
    return {
        "sources": [{"source": r["source"], "item_type": r["item_type"], "events": r["n"], "last": r["last"].isoformat()} for r in rows],
        "not_connected": [s for s in known if s not in present],
        "semantic_search": (await get_embedder()) is not None,
    }
