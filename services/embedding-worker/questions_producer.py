"""Fills the question queue (muse_taste.questions). Optional by design: nothing in Muse waits for
an answer, unanswered questions expire, and a failure here never affects profiles or
recommendations (the caller swallows exceptions)."""

from __future__ import annotations

import logging
import uuid

import asyncpg
import numpy as np
from muse_taste.elicitation import onboarding_spread, this_or_that
from muse_taste.profile import TasteProfile
from muse_taste.questions import (
    MAX_PENDING_PER_USER,
    QuestionSpec,
    abandon_questions,
    interest_questions,
    onboarding_question,
    pair_question,
)
from muse_taste.ranking import CandidateInput, score_candidates
from muse_schema.crypto import P_QUESTION, get_vault
from muse_taste.store import ITEMS_COLLECTION, open_canon_data, open_payload
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logger = logging.getLogger("muse.worker.questions")

COLD_EVIDENCE = 15.0


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


async def expire(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE question_log SET status = 'expired' WHERE status IN ('pending', 'served') AND expires_at IS NOT NULL AND expires_at < now()"
        )
    return int(res.split()[-1]) if res else 0


async def enqueue(pool: asyncpg.Pool, user_id: str, specs: list[QuestionSpec]) -> int:
    added = 0
    vault = get_vault()
    uid = uuid.UUID(user_id)
    async with pool.acquire() as conn:
        for q in specs:
            dedupe = vault.bid(q.dedupe_key, P_QUESTION)
            exists = await conn.fetchval(
                """
                SELECT 1 FROM question_log
                WHERE user_id = $1 AND dedupe_key = $2
                  AND (status IN ('pending', 'served') OR asked_at > now() - make_interval(days => $3::int))
                LIMIT 1
                """,
                uid, dedupe, q.reask_days,
            )
            if exists:
                continue
            await conn.execute(
                """
                INSERT INTO question_log (id, user_id, kind, payload, status, domain, priority, dedupe_key, expires_at)
                VALUES ($1, $2, $3, $4::jsonb, 'pending', $5, $6, $7, now() + make_interval(days => $8::int))
                """,
                uuid.uuid4(), uid, q.kind, vault.seal_json(q.payload, P_QUESTION), q.domain, q.priority, dedupe, q.ttl_days,
            )
            added += 1
        # keep the queue short: beyond the cap, the least valuable pending questions expire
        await conn.execute(
            """
            UPDATE question_log SET status = 'expired' WHERE id IN (
                SELECT id FROM question_log WHERE user_id = $1 AND status = 'pending'
                ORDER BY priority DESC, asked_at DESC OFFSET $2
            )
            """,
            uid, MAX_PENDING_PER_USER,
        )
    return added


def _onboarding_items(qdrant: QdrantClient, prof: TasteProfile, n: int = 18) -> list[dict]:
    flt = qm.Filter(must=[qm.FieldCondition(key="domain", match=qm.MatchValue(value=prof.domain))])
    pts, offset = [], None
    while len(pts) < 3000:
        batch, offset = qdrant.scroll(ITEMS_COLLECTION, scroll_filter=flt, limit=500, offset=offset, with_vectors=True, with_payload=True)
        pts += batch
        if offset is None:
            break
    seen = set(prof.seen)
    opened = [(np.asarray(p.vector, dtype=np.float32), open_payload(p.payload)) for p in pts]
    opened = [(v, pl) for v, pl in opened if pl.get("canon_key") not in seen]
    if not opened:
        return []
    keys = [pl["canon_key"] for _, pl in opened]
    V = np.stack([v for v, _ in opened])
    pop = np.array([(pl.get("vote_count") or 0.0) * (3.0 if pl.get("available") else 1.0) + 1.0 for _, pl in opened])
    by_key = {pl["canon_key"]: pl for _, pl in opened}
    return [_item(by_key[k]) for k in onboarding_spread(keys, V, pop, n=n, baseline=prof.baseline)]


def _pair(qdrant: QdrantClient, prof: TasteProfile, anchor_vectors: dict[str, np.ndarray]) -> QuestionSpec | None:
    if not prof.interests:
        return None
    flt = qm.Filter(must=[qm.FieldCondition(key="domain", match=qm.MatchValue(value=prof.domain)),
                          qm.FieldCondition(key="available", match=qm.MatchValue(value=True))])
    seen = set(prof.seen) | set(prof.in_progress)
    pool: dict[str, tuple] = {}
    for it in prof.interests[:6]:
        for p in qdrant.query_points(ITEMS_COLLECTION, query=it.medoid, query_filter=flt, limit=40, with_vectors=True, with_payload=True).points:
            pl = open_payload(p.payload)
            k = pl.get("canon_key")
            if k and k not in seen:
                pool.setdefault(k, (np.asarray(p.vector, dtype=np.float32), pl))
    if len(pool) < 2:
        return None
    cands = [CandidateInput(key=k, vector=v, tags=pl.get("tags") or [], vote_average=pl.get("vote_average"), vote_count=pl.get("vote_count"), payload=pl)
             for k, (v, pl) in pool.items()]
    scored = score_candidates(prof, cands, anchor_vectors=anchor_vectors)
    pair = this_or_that(sorted(scored, key=lambda s: -s.score)[:200], prof.baseline)
    if pair is None:
        return None
    a, b = pair
    return pair_question(prof.domain, _item(a.payload), _item(b.payload))


async def produce(pool: asyncpg.Pool, qdrant: QdrantClient, prof: TasteProfile, titles: dict[str, str], anchor_vectors: dict[str, np.ndarray]) -> int:
    specs: list[QuestionSpec] = []
    specs += interest_questions(prof, titles)
    if prof.abandoned:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT canon_key, item_type, available, data FROM canon_item WHERE canon_key = ANY($1::text[])", prof.abandoned[:5])
        items = {r["canon_key"]: _item({**open_canon_data(r["data"]), "canon_key": r["canon_key"], "item_type": r["item_type"], "available": r["available"]}) for r in rows}
        specs += abandon_questions(prof, {**titles, **{k: v["title"] for k, v in items.items()}}, items)
    cold = prof.total_confidence < COLD_EVIDENCE
    q = onboarding_question(prof.domain, _onboarding_items(qdrant, prof), cold) if cold or len(prof.interests) <= 1 else None
    if q:
        specs.append(q)
    pq = _pair(qdrant, prof, anchor_vectors)
    if pq:
        specs.append(pq)
    added = await enqueue(pool, prof.user_id, specs)
    if added:
        logger.info("questions: queued %d for user=%s domain=%s", added, prof.user_id, prof.domain)
    return added
