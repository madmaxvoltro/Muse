"""Question selection (muse_taste.elicitation). Answers are written through the ingestion
API's /answers endpoint (write path), which turns them into events."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg

import numpy as np
from muse_taste.canon import domain_of
from muse_taste.elicitation import ABANDON_REASONS, onboarding_spread, this_or_that
from muse_taste.questions import ANSWER_TEMPLATES
from muse_taste.store import ITEMS_COLLECTION
from qdrant_client.http import models as qm

from .engine import TARGET_TYPES, Engine, Request

ONBOARDING_CONFIDENCE = 15.0  # below this much total evidence, onboarding beats pairwise questions


def _item(pl: dict) -> dict:
    refs = pl.get("library_refs") or []
    return {
        "canon_key": pl.get("canon_key"),
        "title": pl.get("title"),
        "year": pl.get("year"),
        "item_type": pl.get("item_type"),
        "available": bool(pl.get("available")),
        "source": refs[0]["source"] if refs else None,
        "source_item_id": refs[0]["source_item_id"] if refs else None,
    }


async def _log(engine: Engine, user_id: str, kind: str, payload: dict) -> str:
    """On-demand questions are logged as already served (so answers can reference them and
    they aren't asked again), never as pending queue entries."""
    qid = uuid.uuid4()
    async with engine.pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO question_log (id, user_id, kind, payload, status, served_at) VALUES ($1, $2, $3, $4::jsonb, 'served', now())",
                qid, uuid.UUID(user_id), kind, payload,
            )
        except asyncpg.UndefinedColumnError:  # database without migration 009
            await conn.execute("INSERT INTO question_log (id, user_id, kind, payload) VALUES ($1, $2, $3, $4::jsonb)", qid, uuid.UUID(user_id), kind, payload)
    return str(qid)


async def queue(engine: Engine, user_id: str, limit: int = 5, domain: str | None = None, kinds: list[str] | None = None,
                mark_served: bool = True, include_served: bool = True) -> dict:
    """The background-filled queue (worker: questions_producer), most valuable first. Fetching
    is optional and so is answering; unanswered questions expire on their own."""
    statuses = ["pending", "served"] if include_served else ["pending"]
    async with engine.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind, domain, priority, payload, status, asked_at, expires_at FROM question_log
            WHERE user_id = $1 AND status = ANY($2::text[]) AND (expires_at IS NULL OR expires_at > now())
              AND ($3::text IS NULL OR domain = $3) AND ($4::text[] IS NULL OR kind = ANY($4::text[]))
            ORDER BY (status = 'pending') DESC, priority DESC, asked_at
            LIMIT $5
            """,
            uuid.UUID(user_id), statuses, domain, kinds, limit,
        )
        if mark_served and rows:
            await conn.execute(
                "UPDATE question_log SET status = 'served', served_at = now() WHERE id = ANY($1::uuid[]) AND status = 'pending'",
                [r["id"] for r in rows],
            )
    return {
        "user_id": user_id,
        "questions": [
            {
                "question_id": str(r["id"]),
                "kind": r["kind"],
                "domain": r["domain"],
                "priority": round(r["priority"], 3),
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                **(r["payload"] or {}),
                "answer_with": {"POST": "/answers (ingestion API, muse key)",
                                "body": {**ANSWER_TEMPLATES.get(r["kind"], {}), "user_id": user_id, "question_id": str(r["id"])},
                                "or_dismiss": {"kind": "dismiss", "user_id": user_id, "question_id": str(r["id"])}},
            }
            for r in rows
        ],
        "note": "Answering is optional; unanswered questions expire and change nothing.",
    }


async def _asked(engine: Engine, user_id: str, kind: str, days: int = 90) -> set[str]:
    async with engine.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM question_log WHERE user_id = $1 AND kind = $2 AND asked_at > now() - make_interval(days => $3::int)",
            uuid.UUID(user_id), kind, days,
        )
    out: set[str] = set()
    for r in rows:
        out.update((r["payload"] or {}).get("keys", []))
    return out


async def onboarding(engine: Engine, user_id: str, item_type: str, n: int = 24) -> dict:
    types = TARGET_TYPES.get(item_type, [item_type])
    prof = await engine.profile(user_id, domain_of(types[0]))
    exclude = set(prof.seen) if prof else set()
    exclude |= await _asked(engine, user_id, "onboarding", days=30)

    def collect():
        flt = qm.Filter(must=[qm.FieldCondition(key="item_type", match=qm.MatchAny(any=types))])
        pts, offset = [], None
        while len(pts) < 4000:
            batch, offset = engine.qdrant.scroll(ITEMS_COLLECTION, scroll_filter=flt, limit=500, offset=offset, with_vectors=True, with_payload=True)
            pts += batch
            if offset is None:
                break
        return pts

    pts = await asyncio.to_thread(collect)
    pts = [p for p in pts if (p.payload or {}).get("canon_key") not in exclude]
    if not pts:
        return {"question_id": None, "kind": "onboarding", "items": []}
    keys = [p.payload["canon_key"] for p in pts]
    V = np.stack([np.asarray(p.vector, dtype=np.float32) for p in pts])
    # Library titles first (people judge what they can watch); TMDB popularity as recognisability.
    pop = np.array([(p.payload.get("vote_count") or 0.0) * (3.0 if p.payload.get("available") else 1.0) + 1.0 for p in pts])
    picks = onboarding_spread(keys, V, pop, n=n, baseline=prof.baseline if prof else 0.2)
    by_key = {p.payload["canon_key"]: p.payload for p in pts}
    qid = await _log(engine, user_id, "onboarding", {"keys": picks, "item_type": item_type})
    return {
        "question_id": qid,
        "kind": "onboarding",
        "prompt": "Tap the titles you love. Mark any that aren't for you.",
        "answers": ["loved", "liked", "disliked", "unknown"],
        "items": [_item(by_key[k]) for k in picks],
    }


async def pairwise(engine: Engine, user_id: str, item_type: str) -> dict:
    res = await engine.recommend(Request(user_id=user_id, item_type=item_type, limit=1, log=False, include_unavailable=False, explore_ratio=0.0))
    if res.profile is None or not res.pool:
        return {"question_id": None, "kind": "this_or_that"}
    asked = await _asked(engine, user_id, "this_or_that")
    pair = this_or_that(res.pool[:300], res.profile.baseline, asked)
    if pair is None:
        return {"question_id": None, "kind": "this_or_that"}
    a, b = pair
    qid = await _log(engine, user_id, "this_or_that", {"keys": [a.key, b.key], "item_type": item_type})
    return {
        "question_id": qid,
        "kind": "this_or_that",
        "prompt": "Which would you rather watch?",
        "answers": ["a", "b", "both", "neither", "skip"],
        "a": {**_item({**a.payload, "canon_key": a.key}), "why_asking": round(a.sigma, 3)},
        "b": {**_item({**b.payload, "canon_key": b.key}), "why_asking": round(b.sigma, 3)},
    }


async def abandoned(engine: Engine, user_id: str, item_type: str, limit: int = 3) -> dict:
    types = TARGET_TYPES.get(item_type, [item_type])
    prof = await engine.profile(user_id, domain_of(types[0]))
    if not prof or not prof.abandoned:
        return {"kind": "abandon", "items": []}
    asked = await _asked(engine, user_id, "abandon", days=365)
    keys = [k for k in prof.abandoned if k not in asked][:limit]
    if not keys:
        return {"kind": "abandon", "items": []}
    async with engine.pool.acquire() as conn:
        rows = await conn.fetch("SELECT canon_key, title, year, item_type, available, library_refs FROM canon_item WHERE canon_key = ANY($1::text[])", keys)
    items = [_item(dict(r)) for r in rows]
    qid = await _log(engine, user_id, "abandon", {"keys": keys, "item_type": item_type})
    return {
        "question_id": qid,
        "kind": "abandon",
        "prompt": "You stopped these early. What happened?",
        "answers": [{"reason": r, "label": label} for r, label in ABANDON_REASONS],
        "items": items,
    }


async def next_question(engine: Engine, user_id: str, item_type: str) -> dict:
    """The single most useful question right now: the top of the background queue if there is
    one, otherwise generated on demand: onboarding while Muse knows little, then 'why did you
    stop?' (cheap, high value), then this-or-that where Muse is least sure."""
    types = TARGET_TYPES.get(item_type, [item_type])
    domain = domain_of(types[0])
    try:
        queued = await queue(engine, user_id, limit=1, domain=domain, include_served=False)
        if queued["questions"]:
            return queued["questions"][0]
    except asyncpg.UndefinedColumnError:
        pass  # database without migration 009: on-demand only
    prof = await engine.profile(user_id, domain)
    if prof is None or prof.total_confidence < ONBOARDING_CONFIDENCE:
        q = await onboarding(engine, user_id, item_type, n=18)
        if q.get("items"):
            return q
    q = await abandoned(engine, user_id, item_type)
    if q.get("items"):
        return q
    return await pairwise(engine, user_id, item_type)
