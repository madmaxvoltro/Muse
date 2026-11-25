"""Muse query API: one place for apps and assistants to ask Muse about a person and to tell it
things. It sits next to the two existing APIs and doesn't replace them:

  reads   -> the recommendation API (recommendations, profiles, the question queue)
  answers -> the ingestion API /answers (the single write path for taste data)
  its own -> people, linked ids, tokens, facts, goals, the current mood/context, settings

Everything about a person that is free text is sealed in the database (muse_schema.crypto).
Docs: docs/api/query-api.md. Auth: see query/auth.py.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query
from muse_schema import identity
from muse_schema.crypto import (
    P_ALIAS,
    P_CONTEXT,
    P_FACT,
    P_GOAL,
    P_LINK_CODE,
    P_PERSON,
    P_QUESTION,
    P_SUGGESTION,
    P_TOKEN_LABEL,
    get_vault,
)
from muse_taste.canon import DOMAIN_OF
from pydantic import BaseModel, Field

from . import clients
from .auth import Principal, admin_only, current, new_token, person, token_digest, writer
from .db import close_pool, get_pool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.query")

app = FastAPI(
    title="Muse Query API",
    version="1.0.0",
    description="Ask Muse about a person (taste, recommendations, questions) and tell it things (answers, facts, mood, goals).",
)

DOMAINS = ("video", "youtube", "music", "books", "general")
DOMAIN_ITEM_TYPES = {"video": ["movie", "series_episode", "series"], "youtube": ["youtube_video"], "music": ["track"],
                     "books": ["audiobook", "ebook"]}
LINK_CODE_MINUTES = 15


def _domain(v: str | None) -> str:
    """Accepts a domain or an item type (track -> music)."""
    if not v:
        return "general"
    v = DOMAIN_OF.get(v, v)
    if v not in DOMAINS:
        raise HTTPException(422, f"domain must be one of {DOMAINS} or an item type")
    return v


def _seal(value: Any, purpose: str) -> Any:
    return get_vault().seal_json(value, purpose)


def _open(value: Any, purpose: str) -> Any:
    return get_vault().open_json(value, purpose) if value is not None else None


def _iso(v) -> str | None:
    return v.isoformat() if v else None


async def _dirty(conn, uid: uuid.UUID) -> None:
    """Rebuild this person's profiles on the worker's next cycle (a fact or a link changed)."""
    await conn.execute("INSERT INTO taste_dirty (user_id) VALUES ($1) ON CONFLICT (user_id) DO UPDATE SET marked_at = now()", uid)


async def _ensure_person(conn, uid: uuid.UUID, display_name: str | None = None) -> None:
    if display_name:
        await conn.execute(
            "INSERT INTO muse_user (user_id, display_name) VALUES ($1, $2::jsonb) ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name",
            uid, _seal(display_name[:120], P_PERSON),
        )
    else:
        await conn.execute("INSERT INTO muse_user (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)


@app.on_event("startup")
async def _startup() -> None:
    get_vault()  # fail fast without a master key


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}


# ---- who ------------------------------------------------------------------------------------

@app.get("/v1/me", tags=["people"])
async def me(user: str | None = None, p: Principal = Depends(current)) -> dict:
    """The person this caller acts for: canonical id, display name and every linked id."""
    if p.admin and not user:
        return {"kind": "admin", "can_write": p.can_write, "user_id": None, "identities": []}
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        ids = await identity.members(conn, uid)
        name = await conn.fetchval("SELECT display_name FROM muse_user WHERE user_id = $1", uid)
        aliases = {r["alias_id"]: r for r in await conn.fetch("SELECT alias_id, label, source FROM user_alias WHERE user_id = $1", uid)}
        counts = await conn.fetch(
            "SELECT user_id, source, count(*) AS n FROM event WHERE user_id = ANY($1::uuid[]) AND action <> 'cataloged' GROUP BY 1, 2", ids)
    by: dict[uuid.UUID, dict[str, int]] = {}
    for r in counts:
        by.setdefault(r["user_id"], {})[r["source"]] = r["n"]
    return {
        "kind": p.kind,
        "can_write": p.can_write,
        "user_id": str(uid),
        "display_name": _open(name, P_PERSON),
        "identities": [
            {"id": str(i), "canonical": i == uid, "label": _open(aliases[i]["label"], P_ALIAS) if i in aliases else None,
             "source": aliases[i]["source"] if i in aliases else None, "events_by_source": by.get(i, {})}
            for i in ids
        ],
    }


@app.get("/v1/users", tags=["people"])
async def users(p: Principal = Depends(admin_only)) -> dict:
    """Everyone Muse knows (admin): canonical ids with their linked ids."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        aliases = await identity.alias_map(conn)
        seen = {str(r["user_id"]) for r in await conn.fetch("SELECT DISTINCT user_id FROM event WHERE action <> 'cataloged'")}
        seen |= {str(r["user_id"]) for r in await conn.fetch("SELECT user_id FROM muse_user")}
        names = {str(r["user_id"]): _open(r["display_name"], P_PERSON) for r in await conn.fetch("SELECT user_id, display_name FROM muse_user")}
    people: dict[str, list[str]] = {}
    for u in seen:
        people.setdefault(aliases.get(u, u), [])
    for a, c in aliases.items():
        people.setdefault(c, []).append(a)
    return {"users": [{"user_id": c, "display_name": names.get(c), "linked_ids": sorted(a)} for c, a in sorted(people.items())]}


class DisplayName(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


@app.put("/v1/users/me/name", tags=["people"])
async def set_name(body: DisplayName, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        await _ensure_person(conn, uid, body.display_name)
    return {"user_id": str(uid), "display_name": body.display_name}


# ---- tokens ---------------------------------------------------------------------------------

class TokenIn(BaseModel):
    label: str = Field("assistant", max_length=120)
    display_name: str | None = Field(None, max_length=120)
    can_write: bool = True
    expires_days: int | None = Field(365, ge=1, le=3650)


@app.post("/v1/tokens", tags=["tokens"])
async def create_token(body: TokenIn, user: str | None = None, household: bool = False, p: Principal = Depends(writer)) -> dict:
    """A bearer token for this person (e.g. for an assistant's MCP connection), shown once.
    An admin may create a household token (`household=true`) that acts for everyone."""
    if p.kind == "person" and p.token_id is not None and body.can_write and not p.can_write:
        raise HTTPException(403, "a read-only token can't create a writing one")
    pool = await get_pool()
    async with pool.acquire() as conn:
        if household:
            if not p.admin:
                raise HTTPException(403, "only an admin can create household tokens")
            uid = None
        else:
            uid = await person(conn, p, user)
            await _ensure_person(conn, uid, body.display_name)
        token = new_token()
        expires = datetime.now(timezone.utc) + timedelta(days=body.expires_days) if body.expires_days else None
        tid = await conn.fetchval(
            "INSERT INTO api_token (token_hash, user_id, label, can_write, expires_at) VALUES ($1, $2, $3::jsonb, $4, $5) RETURNING id",
            token_digest(token), uid, _seal(body.label, P_TOKEN_LABEL), body.can_write, expires,
        )
    return {"id": str(tid), "token": token, "user_id": str(uid) if uid else None, "label": body.label, "can_write": body.can_write,
            "expires_at": _iso(expires), "note": "shown once; Muse only keeps a keyed digest"}


@app.get("/v1/tokens", tags=["tokens"])
async def list_tokens(user: str | None = None, p: Principal = Depends(current)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if p.admin and not user:
            rows = await conn.fetch("SELECT * FROM api_token WHERE revoked_at IS NULL ORDER BY created_at")
        else:
            uid = await person(conn, p, user)
            rows = await conn.fetch("SELECT * FROM api_token WHERE user_id = $1 AND revoked_at IS NULL ORDER BY created_at", uid)
    return {"tokens": [{"id": str(r["id"]), "user_id": str(r["user_id"]) if r["user_id"] else None, "label": _open(r["label"], P_TOKEN_LABEL),
                        "can_write": r["can_write"], "created_at": _iso(r["created_at"]), "last_used_at": _iso(r["last_used_at"]),
                        "expires_at": _iso(r["expires_at"])} for r in rows]}


@app.delete("/v1/tokens/{token_id}", tags=["tokens"])
async def revoke_token(token_id: uuid.UUID, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchrow("SELECT user_id FROM api_token WHERE id = $1 AND revoked_at IS NULL", token_id)
        if owner is None:
            raise HTTPException(404, "no such token")
        if not p.admin and owner["user_id"] != p.user_id:
            raise HTTPException(403, "not your token")
        await conn.execute("UPDATE api_token SET revoked_at = now() WHERE id = $1", token_id)
    return {"revoked": str(token_id)}


# ---- linking identities -----------------------------------------------------------------------

def _norm_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


@app.post("/v1/identity/link-codes", tags=["identity"])
async def link_code(user: str | None = None, p: Principal = Depends(writer)) -> dict:
    """A one-time code (15 minutes). Redeem it while acting as your other account (another app,
    another server) with POST /v1/identity/link and both become one person in Muse."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=LINK_CODE_MINUTES)
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        await conn.execute("DELETE FROM link_code WHERE user_id = $1 AND used_at IS NULL", uid)  # one live code at a time
        await conn.execute("INSERT INTO link_code (code_hash, user_id, expires_at) VALUES ($1, $2, $3)",
                           get_vault().digest(_norm_code(code), P_LINK_CODE), uid, expires)
    return {"code": code, "expires_at": _iso(expires)}


class LinkIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    label: str | None = Field(None, max_length=120)


@app.post("/v1/identity/link", tags=["identity"])
async def link(body: LinkIn, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    """Join the caller's identity (and everything already linked to it) to the person who
    created the code. Their histories are read together from now on; nothing is rewritten."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        me_ = await person(conn, p, user)
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE link_code SET used_at = now(), used_by = $2 WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now() RETURNING user_id",
                get_vault().digest(_norm_code(body.code), P_LINK_CODE), me_,
            )
            if row is None:
                raise HTTPException(404, "unknown or expired code")
            target = await identity.canonical(conn, row["user_id"])
            if target == me_:
                raise HTTPException(409, "that code is your own")
            moved = [r["alias_id"] for r in await conn.fetch("SELECT alias_id FROM user_alias WHERE user_id = $1", me_)]
            await conn.execute("UPDATE user_alias SET user_id = $2 WHERE user_id = $1", me_, target)
            await conn.execute(
                "INSERT INTO user_alias (alias_id, user_id, source, label, linked_by) VALUES ($1, $2, $3, $4::jsonb, 'link_code') "
                "ON CONFLICT (alias_id) DO UPDATE SET user_id = EXCLUDED.user_id",
                me_, target, p.source, _seal(body.label, P_ALIAS) if body.label else None,
            )
            # What was told to the old identity now belongs to the person.
            for t in ("profile_fact", "user_goal"):
                await conn.execute(f"UPDATE {t} SET user_id = $2 WHERE user_id = $1", me_, target)
            await conn.execute("UPDATE api_token SET user_id = $2 WHERE user_id = $1", me_, target)
            await _ensure_person(conn, target)
            await _dirty(conn, target)
    return {"linked": str(me_), "to": str(target), "also_moved": [str(m) for m in moved]}


@app.delete("/v1/identity/aliases/{alias_id}", tags=["identity"])
async def unlink(alias_id: uuid.UUID, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        res = await conn.execute("DELETE FROM user_alias WHERE alias_id = $1 AND user_id = $2", alias_id, uid)
        if res.endswith(" 0"):
            raise HTTPException(404, "not one of your linked ids")
        await _dirty(conn, uid)
        await _dirty(conn, alias_id)
    return {"unlinked": str(alias_id)}


# ---- facts --------------------------------------------------------------------------------------

class FactIn(BaseModel):
    statement: str = Field(min_length=3, max_length=500, description="natural language, e.g. 'Loves slow, sad Dutch songs'")
    kind: Literal["likes", "dislikes", "avoid", "context", "fact"] = "fact"
    domain: str = Field("general", description="video | youtube | music | books | general, or an item type such as track")
    confidence: float = Field(0.7, ge=0, le=1)


def _fact_json(r) -> dict:
    return {"id": str(r["id"]), "kind": r["kind"], "domain": r["domain"], "statement": _open(r["statement"], P_FACT), "subject": None,
            "confidence": r["confidence"], "source": r["source"], "created_at": _iso(r["created_at"])}


@app.get("/v1/users/me/facts", tags=["knowledge"])
async def facts(user: str | None = None, domain: str | None = None, p: Principal = Depends(current)) -> dict:
    """What Muse was told about this person (by them or an assistant)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        rows = await conn.fetch(
            "SELECT * FROM profile_fact WHERE user_id = $1 AND retracted_at IS NULL AND ($2::text IS NULL OR domain = $2 OR domain = 'general') ORDER BY created_at DESC",
            uid, _domain(domain) if domain else None)
    return {"facts": [_fact_json(r) for r in rows]}


@app.post("/v1/users/me/facts", tags=["knowledge"])
async def add_fact(body: FactIn, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    """Tell Muse something. `likes` facts become interests in that domain's taste profile on
    the worker's next cycle, so "loves 80s Italo disco" pulls such music up even before it was
    played. Everything is visible and deletable by the person."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        await _ensure_person(conn, uid)
        row = await conn.fetchrow(
            "INSERT INTO profile_fact (user_id, kind, domain, statement, confidence, source) VALUES ($1, $2, $3, $4::jsonb, $5, $6) RETURNING *",
            uid, body.kind, _domain(body.domain), _seal(body.statement.strip(), P_FACT), body.confidence,
            "assistant" if p.kind == "person" else (p.source or "admin"),
        )
        if body.kind == "likes":
            await _dirty(conn, uid)
    return _fact_json(row)


@app.delete("/v1/users/me/facts/{fact_id}", tags=["knowledge"])
async def forget_fact(fact_id: uuid.UUID, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        kind = await conn.fetchval(
            "UPDATE profile_fact SET retracted_at = now() WHERE id = $1 AND user_id = $2 AND retracted_at IS NULL RETURNING kind", fact_id, uid)
        if kind is None:
            raise HTTPException(404, "no such fact")
        if kind == "likes":
            await _dirty(conn, uid)
    return {"forgotten": str(fact_id)}


# ---- questions ----------------------------------------------------------------------------------

REASONS = {
    "interest": "Muse noticed this in what you play and wants to know if it should follow it.",
    "tag": "Muse is unsure how much this matters to you.",
    "abandon": "You stopped early; knowing why keeps Muse from drawing the wrong conclusion.",
    "this_or_that": "Muse is least sure about these two for you.",
}
PAIR_LABELS = {"both": "Both", "neither": "Neither"}


def _card(q: dict) -> dict | None:
    """A queued question in the shape apps show (Mono's card): one prompt, a few options."""
    kind = q.get("kind")
    item = None
    if kind in ("interest", "tag"):
        options = [{"value": a["response"], "label": a["label"]} for a in q.get("answers") or []]
        ex = (q.get("interest") or {}).get("examples") or [e.get("title") for e in q.get("examples") or [] if isinstance(e, dict)]
        context = {"examples": [e for e in ex if e][:4]}
    elif kind == "abandon":
        options = [{"value": a["reason"], "label": a["label"]} for a in q.get("answers") or []]
        it = (q.get("items") or [{}])[0]
        item = {"source": it.get("source"), "source_item_id": it.get("source_item_id"), "title": it.get("title")} if it else None
        context = {}
    elif kind == "this_or_that":
        a, b = q.get("a") or {}, q.get("b") or {}
        options = [{"value": "a", "label": a.get("title") or "The first"}, {"value": "b", "label": b.get("title") or "The second"},
                   *({"value": k, "label": v} for k, v in PAIR_LABELS.items())]
        context = {"a": a, "b": b}
    else:
        return None  # onboarding (many titles at once) is for clients that show a grid
    return {"id": q["question_id"], "domain": q.get("domain") or "general", "kind": "choice", "muse_kind": kind,
            "prompt": q.get("prompt") or "", "options": options, "reason": REASONS.get(kind), "item": item, "context": context,
            "expires_at": q.get("expires_at")}


@app.get("/v1/users/me/questions", tags=["questions"])
async def questions(user: str | None = None, limit: int = Query(3, ge=1, le=10), domain: str | None = None, raw: bool = False,
                    p: Principal = Depends(current)) -> dict:
    """Questions Muse would like this person to answer, most valuable first. Answering is
    always optional; unanswered questions expire and change nothing. `raw=true` returns the
    recommendation API's own format (with onboarding grids and exact answer bodies)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
    body = await clients.reco(f"/questions/{uid}/queue", {"limit": min(limit * 3, 30), "domain": _domain(domain) if domain else None})
    qs = body.get("questions") or []
    if raw:
        return {"questions": qs[:limit]}
    cards = [c for c in (_card(q) for q in qs) if c]
    return {"questions": cards[:limit], "note": "Answering is optional; unanswered questions expire and change nothing."}


class AnswerIn(BaseModel):
    option: str | None = Field(None, max_length=60, description="the chosen option's value")
    text: str | None = Field(None, max_length=500, description="optional free text; kept as a fact")
    answer: dict | None = Field(None, description="advanced: the exact /answers body (onboarding grids)")


async def _question(conn, qid: str, uid: uuid.UUID) -> dict:
    try:
        row = await conn.fetchrow("SELECT user_id, kind, domain, payload, status FROM question_log WHERE id = $1", uuid.UUID(qid))
    except ValueError:
        raise HTTPException(404, "unknown question") from None
    if row is None or row["user_id"] != uid:
        raise HTTPException(404, "unknown question")
    return {**dict(row), "payload": _open(row["payload"], P_QUESTION) or {}}


def _answer_body(q: dict, body: AnswerIn) -> dict:
    kind, pl = q["kind"], q["payload"]
    item_type = DOMAIN_ITEM_TYPES.get(q["domain"] or "video", ["movie"])[0]
    if body.answer:
        return {**body.answer, "kind": kind}
    opt = body.option
    if not opt:
        raise HTTPException(422, "pick an option (or send `answer`)")
    if kind in ("interest", "tag"):
        allowed = {a["response"] for a in pl.get("answers") or []}
        if opt not in allowed:
            raise HTTPException(422, f"option must be one of {sorted(allowed)}")
        return {"kind": kind, "response": opt}
    if kind == "abandon":
        items = pl.get("items") or []
        if not items or not items[0].get("canon_key"):
            raise HTTPException(422, "question has no title")
        return {"kind": "abandon", "canon_key": items[0]["canon_key"], "reason": opt, "item_type": item_type}
    if kind == "this_or_that":
        if opt not in ("a", "b", "both", "neither", "skip"):
            raise HTTPException(422, "option must be a, b, both, neither or skip")
        return {"kind": "this_or_that", "a": (pl.get("a") or {}).get("canon_key"), "b": (pl.get("b") or {}).get("canon_key"),
                "answer": opt, "item_type": item_type}
    raise HTTPException(422, f"{kind} questions are answered with `answer`")


@app.post("/v1/questions/{question_id}/answer", tags=["questions"])
async def answer(question_id: str, body: AnswerIn, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        q = await _question(conn, question_id, uid)
    out = await clients.answer({**_answer_body(q, body), "user_id": str(uid), "question_id": question_id})
    fact = None
    if body.text and body.text.strip():
        fact = await add_fact(FactIn(statement=body.text.strip()[:500], kind="fact", domain=q["domain"] or "general"), str(uid), p)
    return {"status": "answered", "events": out.get("events", 0), **({"fact": fact} if fact else {})}


@app.post("/v1/questions/{question_id}/skip", tags=["questions"])
async def skip(question_id: str, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        await _question(conn, question_id, uid)
    await clients.answer({"kind": "dismiss", "user_id": str(uid), "question_id": question_id})
    return {"status": "skipped"}


# ---- context, mood, incognito, settings ------------------------------------------------------

class ContextIn(BaseModel):
    kind: str = Field(max_length=40, description="mood | device | weather | calendar | home | activity | any short name")
    value: dict = Field(description='e.g. {"mood": "rainy evening, calm"} or {"device": "car"}')
    ttl_minutes: int = Field(180, ge=1, le=10080)


async def _context_row(conn, uid: uuid.UUID) -> tuple[str | None, datetime | None, dict]:
    row = await conn.fetchrow("SELECT mood, mood_until, signals FROM user_context WHERE user_id = $1", uid)
    if row is None:
        return None, None, {}
    return _open(row["mood"], P_CONTEXT), row["mood_until"], _open(row["signals"], P_CONTEXT) or {}


@app.get("/v1/users/me/context", tags=["context"])
async def context(user: str | None = None, p: Principal = Depends(current)) -> dict:
    """The current situation Muse uses: a mood (used by recommendations that name none) and
    context signals from apps, Home Assistant or an assistant. Expired entries are left out."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        mood, until, signals = await _context_row(conn, uid)
    now = datetime.now(timezone.utc)
    out = []
    if mood and until and until > now:
        out.append({"kind": "mood", "value": {"mood": mood}, "until": _iso(until), "user_id": str(uid)})
    for kind, s in sorted(signals.items()):
        if kind != "mood" and s.get("until") and datetime.fromisoformat(s["until"]) > now:
            out.append({"kind": kind, "value": s.get("value") or {}, "until": s["until"], "user_id": str(uid)})
    return {"signals": out}


@app.post("/v1/users/me/context", tags=["context"])
async def set_context(body: ContextIn, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    until = datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        mood, mood_until, signals = await _context_row(conn, uid)
        if body.kind == "mood":
            text = str(body.value.get("mood") or "").strip()[:200]
            if not text:
                raise HTTPException(422, 'a mood needs value {"mood": "..."}')
            mood, mood_until = text, until
        else:
            signals[body.kind] = {"value": body.value, "until": until.isoformat()}
            if len(str(signals)) > 20000:
                raise HTTPException(413, "too much context")
        await conn.execute(
            """INSERT INTO user_context (user_id, mood, mood_until, signals, updated_at) VALUES ($1, $2::jsonb, $3, $4::jsonb, now())
               ON CONFLICT (user_id) DO UPDATE SET mood = EXCLUDED.mood, mood_until = EXCLUDED.mood_until, signals = EXCLUDED.signals, updated_at = now()""",
            uid, _seal(mood, P_CONTEXT) if mood else None, mood_until, _seal(signals, P_CONTEXT),
        )
    return {"kind": body.kind, "until": _iso(until)}


@app.delete("/v1/users/me/context/{kind}", tags=["context"])
async def clear_context(kind: str, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        if kind == "mood":
            await conn.execute("UPDATE user_context SET mood = NULL, mood_until = NULL, updated_at = now() WHERE user_id = $1", uid)
        else:
            mood, until, signals = await _context_row(conn, uid)
            if signals.pop(kind, None) is not None:
                await conn.execute("UPDATE user_context SET signals = $2::jsonb, updated_at = now() WHERE user_id = $1", uid, _seal(signals, P_CONTEXT))
    return {"cleared": kind}


async def _preferences(conn, uid: uuid.UUID) -> dict:
    return await conn.fetchval("SELECT preferences FROM user_overlay WHERE user_id = $1", uid) or {}


async def _set_preferences(conn, uid: uuid.UUID, prefs: dict) -> None:
    await conn.execute(
        "INSERT INTO user_overlay (user_id, preferences) VALUES ($1, $2::jsonb) ON CONFLICT (user_id) DO UPDATE SET preferences = EXCLUDED.preferences",
        uid, prefs,
    )


@app.get("/v1/users/me/settings", tags=["context"])
async def settings(user: str | None = None, p: Principal = Depends(current)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        prefs = await _preferences(conn, uid)
    until = prefs.get("incognito_until")
    if until and datetime.fromisoformat(until) <= datetime.now(timezone.utc):
        prefs = {k: v for k, v in prefs.items() if k != "incognito_until"}
    return {"user_id": str(uid), "preferences": prefs}


class IncognitoIn(BaseModel):
    minutes: int = Field(60, ge=0, le=10080, description="0 ends incognito")


@app.put("/v1/users/me/incognito", tags=["context"])
async def incognito(body: IncognitoIn, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    """While incognito, what this person plays isn't learned from (the ingestion API drops it,
    for every linked id). Answers and things they tell Muse still count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        prefs = await _preferences(conn, uid)
        if body.minutes:
            prefs["incognito_until"] = (datetime.now(timezone.utc) + timedelta(minutes=body.minutes)).isoformat()
        else:
            prefs.pop("incognito_until", None)
        await _set_preferences(conn, uid, prefs)
    return {"incognito_until": prefs.get("incognito_until")}


# ---- goals --------------------------------------------------------------------------------------

class GoalIn(BaseModel):
    title: str = Field(min_length=2, max_length=200, description="e.g. 'Discover 20 new artists'")
    domain: str = Field("music", description="a domain or item type")
    target: int = Field(10, ge=1, le=100000, description="how many new titles to finish")
    until: datetime | None = None


async def _goal_json(conn, uid: uuid.UUID, r) -> dict:
    d = _open(r["data"], P_GOAL) or {}
    types = DOMAIN_ITEM_TYPES.get(d.get("domain") or "music", ["track"])
    ids = await identity.members(conn, uid)
    # progress: different titles finished (or loved) since the goal was set, by arrival time
    # (client clocks can be off)
    progress = await conn.fetchval(
        """SELECT count(DISTINCT source_item_id) FROM event WHERE user_id = ANY($1::uuid[]) AND item_type = ANY($2::text[])
           AND action IN ('completed', 'favorited', 'added_to_playlist') AND ingested_at >= $3""",
        ids, types, r["created_at"],
    )
    target = int(d.get("target") or 1)
    status = "done" if r["done_at"] or progress >= target else "active"
    return {"id": str(r["id"]), "title": d.get("title"), "domain": d.get("domain"), "target": target, "progress": int(progress),
            "until": d.get("until"), "status": status, "created_at": _iso(r["created_at"])}


@app.get("/v1/users/me/goals", tags=["goals"])
async def goals(user: str | None = None, p: Principal = Depends(current)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        rows = await conn.fetch("SELECT * FROM user_goal WHERE user_id = $1 AND deleted_at IS NULL ORDER BY created_at DESC", uid)
        return {"goals": [await _goal_json(conn, uid, r) for r in rows]}


@app.post("/v1/users/me/goals", tags=["goals"])
async def add_goal(body: GoalIn, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        data = {"title": body.title, "domain": _domain(body.domain), "target": body.target, "until": _iso(body.until)}
        r = await conn.fetchrow("INSERT INTO user_goal (user_id, data) VALUES ($1, $2::jsonb) RETURNING *", uid, _seal(data, P_GOAL))
        return await _goal_json(conn, uid, r)


@app.delete("/v1/users/me/goals/{goal_id}", tags=["goals"])
async def delete_goal(goal_id: uuid.UUID, user: str | None = None, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
        res = await conn.execute("UPDATE user_goal SET deleted_at = now() WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL", goal_id, uid)
    if res.endswith(" 0"):
        raise HTTPException(404, "no such goal")
    return {"deleted": str(goal_id)}


# ---- taste ---------------------------------------------------------------------------------------

@app.get("/v1/users/me/taste-portrait", tags=["taste"])
async def portrait(user: str | None = None, item_type: list[str] | None = Query(None), p: Principal = Depends(current)) -> dict:
    """In words: per domain the summary, the interests (the 'sound' of your music), loved and
    disliked titles and how your taste is moving."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
    wanted = item_type or ["movie", "track", "youtube_video", "audiobook"]
    out: dict[str, dict] = {}
    for it in wanted:
        prof = await clients.reco(f"/profile/{uid}", {"domain": _domain(it)}, missing_ok=True)
        if not prof:
            continue
        evo = await clients.reco(f"/evolution/{uid}", {"domain": _domain(it)}, missing_ok=True) or {}
        states = {i["name"]: i.get("state") for i in evo.get("interests") or []}
        out[it] = {
            "summary": prof.get("summary") or "",
            "sound": [{"label": i.get("label") or ", ".join(i.get("top_tags") or [])[:80] or "untitled", "share": i.get("share", 0),
                       "examples": i.get("examples", [])[:4], "state": states.get(i.get("label"))} for i in prof.get("interests") or []],
            "loved": [t["title"] for t in prof.get("top_titles") or []][:10],
            "disliked": (prof.get("disliked") or [])[:10],
            "stats": prof.get("stats") or {},
            "narrative": evo.get("narrative"),
        }
    return {"user_id": str(uid), "domains": out}


@app.get("/v1/users/me/recommendations", tags=["taste"])
async def recommendations(item_type: str = "movie", limit: int = Query(10, ge=1, le=50), mood: str | None = Query(None, max_length=200),
                          user: str | None = None, p: Principal = Depends(current)) -> dict:
    """Explained recommendations (the recommendation API's, for this person and their mood)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await person(conn, p, user)
    return await clients.reco(f"/recommendations/{uid}", {"item_type": item_type, "limit": limit, "mood": mood, "surface": "api", "log": "false"})


@app.get("/v1/users/me/releases", tags=["taste"])
async def releases(user: str | None = None, p: Principal = Depends(current)) -> dict:
    """New and upcoming releases by artists this person loves (needs the catalog service)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await person(conn, p, user)
    return {"releases": [], "note": "release tracking arrives with the music catalog service"}


# ---- ideas for Muse ------------------------------------------------------------------------------

class SuggestionIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=2000)
    rationale: str | None = Field(None, max_length=1000)


@app.post("/v1/suggestions", tags=["ideas"])
async def suggest(body: SuggestionIn, p: Principal = Depends(writer)) -> dict:
    """An idea for Muse itself, e.g. from an assistant that noticed something missing."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        sid = await conn.fetchval("INSERT INTO feature_suggestion (user_id, data, created_by) VALUES ($1, $2::jsonb, $3) RETURNING id",
                                  p.user_id, _seal(body.model_dump(), P_SUGGESTION), p.kind)
    return {"id": str(sid), "status": "new"}


@app.get("/v1/suggestions", tags=["ideas"])
async def suggestions(p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM feature_suggestion ORDER BY created_at DESC LIMIT 200")
    return {"suggestions": [{"id": str(r["id"]), **(_open(r["data"], P_SUGGESTION) or {}), "status": r["status"], "created_by": r["created_by"],
                             "created_at": _iso(r["created_at"])} for r in rows]}


@app.exception_handler(asyncpg.UndefinedTableError)
async def _no_migration(request, exc):  # pragma: no cover - deployment error
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "database is missing migration 011: run ./muse upgrade"}, status_code=503)
