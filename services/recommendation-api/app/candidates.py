"""Stage 1: candidate generation, and the feature-gathering that feeds stage 2 scoring.

v1 implements the vector-similarity generator only (Qdrant taste-vector search). The
other generators described in docs/architecture.md — item-item co-occurrence,
cross-source backlog signal, trending/new-arrival injection — are not yet implemented;
see the TODOs below. Similarity-only is enough to serve real recommendations, the
others widen recall.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from muse_schema import content_point_id, taste_point_id
from qdrant_client import QdrantClient
from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from .scoring import Candidate, confidence

CANDIDATE_POOL_SIZE = 200


async def get_owned_item_ids(pool: asyncpg.Pool, user_id: str) -> set[str]:
    """Items the user has already interacted with — excluded from candidates,
    they belong in "continue watching", not "recommended for you".
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT source, source_item_id FROM event WHERE user_id = $1 AND source_item_id IS NOT NULL",
            uuid.UUID(user_id),
        )
    return {f"{r['source']}:{r['source_item_id']}" for r in rows}


async def get_user_genre_stats(pool: asyncpg.Pool, qdrant: QdrantClient, user_id: str) -> dict[str, dict[str, Any]]:
    """Per-genre stats derived from the user's recent history, used as the feature
    basis for scoring candidates that share a genre. See Candidate.within_user_popularity
    / completion_rate / recency_of_similar / confidence in scoring.py.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source, source_item_id, action, action_weight, "timestamp"
            FROM event
            WHERE user_id = $1 AND source_item_id IS NOT NULL
            ORDER BY "timestamp" DESC
            LIMIT 500
            """,
            uuid.UUID(user_id),
        )
    if not rows:
        return {}

    point_ids = [content_point_id(r["source"], r["source_item_id"]) for r in rows]
    fetched = qdrant.retrieve(collection_name="content_items", ids=point_ids, with_payload=True)
    genres_by_point = {p.id: (p.payload or {}).get("genres", []) for p in fetched}

    now = datetime.now(timezone.utc)
    stats: dict[str, dict[str, Any]] = {}
    for row, pid in zip(rows, point_ids):
        genres = genres_by_point.get(pid) or []
        age_days = max((now - row["timestamp"]).total_seconds() / 86400.0, 0.0)
        for g in genres:
            s = stats.setdefault(
                g, {"count": 0, "weights": [], "ages": [], "sources": set(), "completions": 0}
            )
            s["count"] += 1
            s["weights"].append(row["action_weight"])
            s["ages"].append(age_days)
            s["sources"].add(row["source"])
            if row["action"] == "completed":
                s["completions"] += 1
    return stats


def genre_features(genres: list[str], genre_stats: dict[str, dict[str, Any]], total_sources: int) -> dict[str, float]:
    if not genres or not genre_stats:
        return {
            "within_user_popularity": 0.0,
            "completion_rate": 0.0,
            "recency_of_similar_days": 365.0,
            "confidence": 0.0,
        }

    matches = [genre_stats[g] for g in genres if g in genre_stats]
    if not matches:
        return {
            "within_user_popularity": 0.0,
            "completion_rate": 0.0,
            "recency_of_similar_days": 365.0,
            "confidence": 0.0,
        }

    total_count = sum(m["count"] for m in matches)
    total_completions = sum(m["completions"] for m in matches)
    min_age = min(min(m["ages"]) for m in matches)
    avg_age = sum(sum(m["ages"]) for m in matches) / total_count
    all_weights = [w for m in matches for w in m["weights"]]
    variance = _variance(all_weights)
    distinct_sources = len({s for m in matches for s in m["sources"]})
    cross_source_agreement = distinct_sources / max(total_sources, 1)

    return {
        "within_user_popularity": min(total_count / 20.0, 1.0),
        "completion_rate": total_completions / total_count if total_count else 0.0,
        "recency_of_similar_days": min_age,
        "confidence": confidence(total_count, variance, avg_age, cross_source_agreement),
    }


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


async def generate_candidates(
    pool: asyncpg.Pool, qdrant: QdrantClient, user_id: str, item_type: str, total_sources: int = 7
) -> list[Candidate]:
    taste_id = taste_point_id(user_id, item_type)
    taste_points = qdrant.retrieve(collection_name="taste_vectors", ids=[taste_id], with_vectors=True)
    if not taste_points:
        # No item_type-specific taste yet — fall back to the combined cross-domain vector.
        taste_points = qdrant.retrieve(
            collection_name="taste_vectors", ids=[taste_point_id(user_id, "_combined")], with_vectors=True
        )
    if not taste_points:
        return []  # cold-start: no taste vector at all yet, nothing to rank against

    owned = await get_owned_item_ids(pool, user_id)
    genre_stats = await get_user_genre_stats(pool, qdrant, user_id)

    hits = qdrant.query_points(
        collection_name="content_items",
        query=taste_points[0].vector,
        query_filter=Filter(must=[FieldCondition(key="item_type", match=MatchValue(value=item_type))]),
        limit=CANDIDATE_POOL_SIZE,
    ).points

    candidates: list[Candidate] = []
    for hit in hits:
        payload = hit.payload or {}
        key = f"{payload.get('source')}:{payload.get('source_item_id')}"
        if key in owned:
            continue

        genres = payload.get("genres", []) or []
        features = genre_features(genres, genre_stats, total_sources)

        candidates.append(
            Candidate(
                source=payload.get("source", ""),
                source_item_id=payload.get("source_item_id", ""),
                item_type=item_type,
                similarity=hit.score,
                recency_of_similar_days=features["recency_of_similar_days"],
                within_user_popularity=features["within_user_popularity"],
                completion_rate=features["completion_rate"],
                confidence=features["confidence"],
                genres=genres,
            )
        )
    return candidates
