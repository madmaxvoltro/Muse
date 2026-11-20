"""Stage-2 features from a user's recent history: per-genre popularity, completion rate,
recency and confidence (scoring.confidence). Candidate generation itself lives in
funnel.py (per-facet, seeded, mood and group searches)."""

import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from muse_schema import content_point_id
from qdrant_client import QdrantClient

from .scoring import confidence


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
