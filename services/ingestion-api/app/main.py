"""Ingestion API: the single write path into the event store.

  POST /events              one event (adapters)
  POST /events/batch        up to 1000 events in one request
  POST /answers             answers to Muse's questions (onboarding, this-or-that, why-did-you-stop)
  POST /import/{kind}       upload a history export (YouTube Takeout, Spotify, Letterboxd, IMDb,
                            Netflix, Trakt, SoundCloud); the file or the whole .zip as the body

Every key maps to one source (MUSE_ADAPTER_KEYS="jellyfin:k1,muse:k2,import:k3,..."), decided
server-side. Two keys are special: `muse` (the client that shows Muse's questions, e.g. Vesper)
may post answers and imports; `import` may post imports and events claiming an import source
(youtube, spotify, letterboxd, imdb, netflix, trakt, soundcloud), and nothing else.
"""

import json
import logging

import asyncpg
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from muse_schema import DEFAULT_ACTION_WEIGHTS, IMPORT_SOURCES, RATED_WEIGHTS, Event, Source
from pydantic import BaseModel, Field

from .auth import require_adapter_key
from .db import close_pool, get_pool
from .ratelimit import check_rate_limit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.ingestion")

app = FastAPI(title="Muse Ingestion API", version="0.2.0")

MAX_IMPORT_BYTES = 512 * 1024 * 1024
INSERT_SQL = """
INSERT INTO event (
    user_id, source, source_item_id, item_type, action, action_weight,
    "timestamp", duration_ms, progress_pct, metadata, device_context
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
"""

# With metadata.dedupe_key the insert is idempotent per (user, source, key): pollers can
# re-send after a restart (or import history) without double counting.
INSERT_DEDUPED_SQL = """
INSERT INTO event (
    user_id, source, source_item_id, item_type, action, action_weight,
    "timestamp", duration_ms, progress_pct, metadata, device_context
)
SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM event WHERE user_id = $1 AND source = $2 AND metadata->>'dedupe_key' = $12
)
"""


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _effective_source(event: Event, key_source: str) -> None:
    """The key decides the source. `import` may claim an import source; `muse` may claim an
    import source or `muse`; every other key's events get the key's source, whatever the body
    says (a compromised adapter can't spoof another source)."""
    claimed = event.source.value
    if key_source == "import":
        if claimed not in IMPORT_SOURCES:
            raise HTTPException(status_code=403, detail="the import key can only write import sources")
        return
    if key_source == "muse" and (claimed in IMPORT_SOURCES or claimed == "muse"):
        return
    try:
        event.source = Source(key_source)
    except ValueError:
        raise HTTPException(status_code=403, detail=f"key source {key_source!r} cannot write events") from None


async def _insert(events: list[Event]) -> int:
    plain, deduped = [], []
    for e in events:
        weight = e.action_weight if e.action_weight is not None else DEFAULT_ACTION_WEIGHTS[e.action]
        args = [
            e.user_id, e.source.value, e.source_item_id, e.item_type.value, e.action.value, weight, e.timestamp,
            e.duration_ms, e.progress_pct, json.dumps(e.metadata), json.dumps(e.device_context) if e.device_context is not None else None,
        ]
        dedupe = e.metadata.get("dedupe_key")
        (deduped if dedupe is not None else plain).append(args + ([str(dedupe)] if dedupe is not None else []))
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if plain:
                await conn.executemany(INSERT_SQL, plain)
            if deduped:
                await conn.executemany(INSERT_DEDUPED_SQL, deduped)
    return len(events)


@app.post("/events", status_code=202)
async def ingest_event(event: Event, source: str = Depends(require_adapter_key)) -> dict[str, str]:
    """Adapters POST here, authenticated with their own X-Muse-Api-Key.

    The key determines `source` server-side — an adapter cannot claim to be a
    different source than the one it was issued a key for, even if it lies in
    the request body's `source` field. This is intentional defense against a
    compromised adapter spoofing another source's events.
    """
    _effective_source(event, source)
    check_rate_limit(source)
    await _insert([event])
    logger.info("ingested event source=%s action=%s user=%s", event.source.value, event.action.value, event.user_id)
    return {"status": "accepted"}


@app.post("/events/batch", status_code=202)
async def ingest_batch(events: list[Event], source: str = Depends(require_adapter_key)) -> dict:
    if len(events) > 1000:
        raise HTTPException(status_code=413, detail="at most 1000 events per batch")
    for e in events:
        _effective_source(e, source)
    check_rate_limit(source, len(events))
    n = await _insert(events)
    return {"status": "accepted", "count": n}


# ---- answers to Muse's questions --------------------------------------------------------------

class OnboardingAnswer(BaseModel):
    kind: Literal["onboarding"]
    user_id: str
    item_type: str = "movie"
    question_id: str | None = None
    loved: list[str] = Field(default_factory=list)  # canon keys
    liked: list[str] = Field(default_factory=list)
    disliked: list[str] = Field(default_factory=list)


class PairAnswer(BaseModel):
    kind: Literal["this_or_that"]
    user_id: str
    item_type: str = "movie"
    question_id: str | None = None
    a: str
    b: str
    answer: Literal["a", "b", "both", "neither", "skip"]


class AbandonAnswer(BaseModel):
    kind: Literal["abandon"]
    user_id: str
    item_type: str = "movie"
    question_id: str | None = None
    canon_key: str
    reason: Literal["not_for_me", "not_in_mood", "too_slow", "too_intense", "will_finish_later", "watched_elsewhere", "bad_quality"]


class InterestAnswer(BaseModel):
    kind: Literal["interest"]
    user_id: str
    question_id: str  # required: the interest is looked up from the queued question
    response: Literal["more", "keep", "less", "break", "no_break", "bring_back", "moved_on"]


class Dismiss(BaseModel):
    """'Don't ask me this': closes the question, writes no taste data."""

    kind: Literal["dismiss"]
    user_id: str
    question_id: str


DOMAIN_ITEM_TYPE = {"video": "movie", "youtube": "youtube_video", "music": "track", "books": "audiobook"}


def _answer_event(user_id: str, item_type: str, canon: str, action: str, qid: str | None, extra: dict | None = None, weight: float | None = None) -> Event:
    md = {"canon_key": canon, "via": "question", **(extra or {})}
    if qid:
        md["question_id"] = qid
        md["dedupe_key"] = f"answer:{qid}:{canon}:{action}"
    return Event(user_id=user_id, source="muse", source_item_id=canon, item_type=item_type, action=action, action_weight=weight,
                 timestamp=datetime.now(timezone.utc), metadata=md)


@app.post("/answers", status_code=202)
async def answers(body: OnboardingAnswer | PairAnswer | AbandonAnswer | InterestAnswer | Dismiss, source: str = Depends(require_adapter_key)) -> dict:
    """Answers are optional: Muse works the same whether or not anyone answers its questions."""
    if source != "muse":
        raise HTTPException(status_code=403, detail="answers need the `muse` key")
    pool = await get_pool()
    question = None
    if body.question_id:
        async with pool.acquire() as conn:
            question = await conn.fetchrow("SELECT user_id::text AS user_id, kind, payload, domain, status FROM question_log WHERE id::text = $1", body.question_id)
        if question is not None and question["user_id"] != body.user_id:
            raise HTTPException(status_code=403, detail="question belongs to another user")
    evs: list[Event] = []
    if isinstance(body, Dismiss):
        if question is None:
            raise HTTPException(status_code=404, detail="unknown question")
    elif isinstance(body, InterestAnswer):
        if question is None or question["kind"] != "interest":
            raise HTTPException(status_code=404, detail="unknown interest question")
        payload = question["payload"] if isinstance(question["payload"], dict) else json.loads(question["payload"])
        interest = payload.get("interest") or {}
        anchor = interest.get("anchor_key")
        if not anchor:
            raise HTTPException(status_code=422, detail="question has no interest anchor")
        item_type = DOMAIN_ITEM_TYPE.get(question["domain"] or "video", "movie")
        evs.append(_answer_event(body.user_id, item_type, anchor, "interest_feedback", body.question_id,
                                 {"response": body.response, "members": interest.get("members", []), "subkind": payload.get("subkind"), "via": "interest_question"}))
    elif isinstance(body, OnboardingAnswer):
        for keys, value in ((body.loved, 2), (body.liked, 1), (body.disliked, -1)):
            for k in keys:
                evs.append(_answer_event(body.user_id, body.item_type, k, "rated", body.question_id, {"value": value, "via": "onboarding"}, RATED_WEIGHTS[value]))
    elif isinstance(body, PairAnswer):
        pairs = {"a": [(body.a, "chose"), (body.b, "passed_over")], "b": [(body.b, "chose"), (body.a, "passed_over")],
                 "both": [(body.a, "chose"), (body.b, "chose")], "neither": [(body.a, "passed_over"), (body.b, "passed_over")], "skip": []}[body.answer]
        for k, action in pairs:
            weight = -0.35 if body.answer == "neither" else None  # "neither appeals" is a real (mild) no
            evs.append(_answer_event(body.user_id, body.item_type, k, action, body.question_id, {"via": "this_or_that"}, weight))
    else:
        evs.append(_answer_event(body.user_id, body.item_type, body.canon_key, "abandon_reason", body.question_id, {"reason": body.reason}))
    check_rate_limit(source, max(len(evs), 1))
    if evs:
        await _insert(evs)
    if question is not None:
        status = "dismissed" if isinstance(body, Dismiss) else "answered"
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    "UPDATE question_log SET answered_at = now(), status = $2, answer = $3::jsonb WHERE id::text = $1",
                    body.question_id, status, body.model_dump_json(exclude={"user_id"}),
                )
            except asyncpg.UndefinedColumnError:  # database without migration 009
                await conn.execute("UPDATE question_log SET answered_at = now() WHERE id::text = $1", body.question_id)
    return {"status": "accepted", "events": len(evs)}


# ---- history imports ---------------------------------------------------------------------

@app.post("/import/{kind}", status_code=202)
async def import_history(
    kind: str,
    request: Request,
    user_id: str = Query(...),
    filename: str = Query("upload", description="original file name; parsers use it to recognise the export file"),
    profile: str | None = Query(None, description="Netflix: only this profile's viewing"),
    source: str = Depends(require_adapter_key),
) -> dict:
    from muse_taste.importers import KINDS, ImportError_, parse, with_user

    if source not in ("import", "muse"):
        raise HTTPException(status_code=403, detail="imports need the `import` or `muse` key")
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail=f"unknown import kind; one of {list(KINDS)}")
    data = await request.body()
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        raw = parse(kind, filename, data, {"profile": profile} if profile else {})
    except (ImportError_, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"could not parse {kind} export: {exc}") from exc
    events = [Event(**e) for e in with_user(raw, user_id)]
    check_rate_limit("import", len(events))
    for i in range(0, len(events), 2000):
        await _insert(events[i : i + 2000])
    by_action: dict[str, int] = {}
    for e in events:
        by_action[e.action.value] = by_action.get(e.action.value, 0) + 1
    logger.info("import kind=%s user=%s events=%d", kind, user_id, len(events))
    return {"status": "accepted", "kind": kind, "events": len(events), "by_action": by_action,
            "note": "duplicates of earlier imports are skipped; the taste profile updates on the next worker cycle"}
