"""What Muse was told: facts, overlay settings, feature suggestions."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException

from .identity import mark_dirty
from .items import DOMAINS

FACT_KINDS = ("likes", "dislikes", "avoid", "context", "fact")
DEFAULT_SENTIMENT = {"likes": 0.7, "dislikes": -0.7, "avoid": -1.0, "context": 0.0, "fact": 0.0}


def fact_json(r) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "domain": r["domain"],
        "statement": r["statement"],
        "subject": r["subject"],
        "sentiment": round(float(r["sentiment"]), 3),
        "confidence": round(float(r["confidence"]), 3),
        "source": r["source"],
        "created_at": r["created_at"].isoformat(),
    }


async def add_fact(
    conn,
    user_id: uuid.UUID,
    statement: str,
    kind: str = "likes",
    domain: str = "general",
    subject: str | None = None,
    sentiment: float | None = None,
    confidence: float = 0.7,
    source: str = "assistant",
    origin_question: uuid.UUID | None = None,
) -> dict[str, Any]:
    statement = (statement or "").strip()
    if not statement:
        raise HTTPException(422, "statement is empty")
    if len(statement) > 1000:
        raise HTTPException(422, "statement is too long (max 1000 characters); split it into separate facts")
    if kind not in FACT_KINDS:
        raise HTTPException(422, f"kind must be one of {FACT_KINDS}")
    if domain not in DOMAINS:
        raise HTTPException(422, f"domain must be one of {DOMAINS}")
    if sentiment is None:
        sentiment = DEFAULT_SENTIMENT[kind]
    sentiment = max(-1.0, min(1.0, float(sentiment)))
    if kind == "likes" and sentiment < 0 or kind in ("dislikes", "avoid") and sentiment > 0:
        raise HTTPException(422, f"sentiment {sentiment} contradicts kind '{kind}'")
    if kind == "avoid" and not subject:
        raise HTTPException(422, "an 'avoid' fact needs a subject (the artist, genre or title to never suggest)")
    confidence = max(0.0, min(1.0, float(confidence)))
    subject = (subject or "").strip() or None

    # Same statement again: refresh instead of duplicating.
    existing = await conn.fetchrow(
        "SELECT id FROM profile_fact WHERE user_id = $1 AND retracted_at IS NULL AND domain = $2 "
        "AND lower(statement) = lower($3)",
        user_id, domain, statement,
    )
    if existing:
        row = await conn.fetchrow(
            "UPDATE profile_fact SET kind = $2, subject = $3, sentiment = $4, confidence = GREATEST(confidence, $5) "
            "WHERE id = $1 RETURNING *",
            existing["id"], kind, subject, sentiment, confidence,
        )
    else:
        row = await conn.fetchrow(
            "INSERT INTO profile_fact (user_id, kind, domain, statement, subject, sentiment, confidence, source, origin_question) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *",
            user_id, kind, domain, statement, subject, sentiment, confidence, source, origin_question,
        )
    if sentiment != 0 or kind == "avoid":
        await mark_dirty(conn, user_id)
    return fact_json(row)


async def list_facts(conn, user_id: uuid.UUID, domain: str | None = None) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM profile_fact WHERE user_id = $1 AND retracted_at IS NULL "
        "AND ($2::text IS NULL OR domain = $2 OR domain = 'general') ORDER BY created_at DESC",
        user_id, domain,
    )
    return [fact_json(r) for r in rows]


async def retract_fact(conn, user_id: uuid.UUID, fact_id: uuid.UUID) -> dict[str, Any]:
    row = await conn.fetchrow(
        "UPDATE profile_fact SET retracted_at = now() WHERE id = $1 AND user_id = $2 AND retracted_at IS NULL RETURNING *",
        fact_id, user_id,
    )
    if row is None:
        raise HTTPException(404, "no such fact")
    await mark_dirty(conn, user_id)
    return {"retracted": fact_json(row)}


async def get_overlay(conn, user_id: uuid.UUID) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM user_overlay WHERE user_id = $1", user_id)
    if row is None:
        return {"source_weights": {}, "explore_ratio": 0.125, "disabled_sources": [], "preferences": {}}
    return {
        "source_weights": row["source_weights"] or {},
        "explore_ratio": round(float(row["explore_ratio"]), 3),
        "disabled_sources": row["disabled_sources"] or [],
        "preferences": row["preferences"] or {},
    }


async def update_overlay(
    conn,
    user_id: uuid.UUID,
    explore_ratio: float | None = None,
    source_weights: dict[str, float] | None = None,
    disabled_sources: list[str] | None = None,
    preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if explore_ratio is not None and not 0 <= explore_ratio <= 1:
        raise HTTPException(422, "explore_ratio must be between 0 and 1")
    if preferences and "taste_blend" in preferences and not 0 <= float(preferences["taste_blend"]) <= 1:
        raise HTTPException(422, "taste_blend must be between 0 and 1")
    await conn.execute("INSERT INTO user_overlay (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
    await conn.execute(
        """
        UPDATE user_overlay SET
            explore_ratio = COALESCE($2, explore_ratio),
            source_weights = CASE WHEN $3::jsonb IS NULL THEN source_weights ELSE source_weights || $3::jsonb END,
            disabled_sources = COALESCE($4::jsonb, disabled_sources),
            preferences = CASE WHEN $5::jsonb IS NULL THEN preferences ELSE preferences || $5::jsonb END,
            updated_at = now()
        WHERE user_id = $1
        """,
        user_id, explore_ratio, source_weights, disabled_sources, preferences,
    )
    return await get_overlay(conn, user_id)


# --- feature suggestions -------------------------------------------------------------------


def suggestion_json(r) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "title": r["title"], "description": r["description"], "rationale": r["rationale"],
        "status": r["status"], "votes": r["votes"], "created_by": r["created_by"],
        "user_id": str(r["user_id"]) if r["user_id"] else None, "created_at": r["created_at"].isoformat(),
    }


async def suggest_feature(conn, user_id: uuid.UUID | None, title: str, description: str, rationale: str | None, created_by: str) -> dict:
    title, description = title.strip(), description.strip()
    if not title or not description:
        raise HTTPException(422, "title and description are required")
    existing = await conn.fetchrow(
        "UPDATE feature_suggestion SET votes = votes + 1, "
        "rationale = CASE WHEN $2::text IS NULL THEN rationale ELSE concat_ws(E'\\n', rationale, $2::text) END "
        "WHERE lower(title) = lower($1) AND status IN ('new', 'planned') RETURNING *",
        title, rationale,
    )
    if existing:
        return {**suggestion_json(existing), "note": "an identical idea already existed: counted as a vote"}
    row = await conn.fetchrow(
        "INSERT INTO feature_suggestion (user_id, title, description, rationale, created_by) VALUES ($1, $2, $3, $4, $5) RETURNING *",
        user_id, title[:200], description[:4000], rationale, created_by,
    )
    return suggestion_json(row)


async def list_suggestions(conn, status: str | None = None) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM feature_suggestion WHERE ($1::text IS NULL OR status = $1) ORDER BY votes DESC, created_at DESC LIMIT 200",
        status,
    )
    return [suggestion_json(r) for r in rows]
