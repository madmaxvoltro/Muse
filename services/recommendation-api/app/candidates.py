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
from muse_schema.storage import fetch_open
from qdrant_client import QdrantClient
from qdrant_client.http.models import FieldCondition, Filter, MatchAny, MatchValue

from .scoring import Candidate, confidence


class _Conn:
    """`async with _acq(x)`: a pool gives a connection, a connection is used as is."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


def _acq(x):
    return x.acquire() if hasattr(x, "acquire") else _Conn(x)

CANDIDATE_POOL_SIZE = 200


async def get_owned_item_ids(pool: asyncpg.Pool, user_ids: list[uuid.UUID]) -> set[str]:
    """Items the user has already watched or judged — excluded from candidates,
    they belong in "continue watching", not "recommended for you".

    Only consumption and explicit judgements count. `cataloged` (the item merely
    exists in the user's library) and weak browsing signals (detail_viewed,
    searched, trailer_played, added_watchlist, requested) must not, or a
    library-backed client such as Vesper would see every title it owns excluded.
    """
    async with _acq(pool) as conn:
        rows = await fetch_open(
            conn,
            """
            SELECT DISTINCT ON (source, source_item_id) source, source_item_id, metadata FROM event
            WHERE user_id = ANY($1::uuid[]) AND source_item_id IS NOT NULL
              AND action IN ('played', 'completed', 'skipped', 'rejected', 'removed', 'rated', 'favorited',
                             'unfavorited', 'added_to_playlist')
            """,
            user_ids,
        )
    return {f"{r['source']}:{r['source_item_id']}" for r in rows}


async def get_user_genre_stats(
    pool: asyncpg.Pool, qdrant: QdrantClient, user_ids: list[uuid.UUID]
) -> dict[str, dict[str, Any]]:
    """Per-genre stats derived from the user's recent history, used as the feature
    basis for scoring candidates that share a genre. See Candidate.within_user_popularity
    / completion_rate / recency_of_similar / confidence in scoring.py.
    """
    async with _acq(pool) as conn:
        rows = await fetch_open(
            conn,
            """
            SELECT source, source_item_id, action, action_weight, "timestamp", metadata
            FROM event
            WHERE user_id = ANY($1::uuid[]) AND source_item_id IS NOT NULL AND action <> 'cataloged'
            ORDER BY "timestamp" DESC
            LIMIT 500
            """,
            user_ids,
        )
    if not rows:
        return {}

    point_ids = [content_point_id(r["source"], r["source_item_id"]) for r in rows]
    fetched = qdrant.retrieve(collection_name="content_items", ids=point_ids, with_payload=True)
    genres_by_point = {str(p.id): (p.payload or {}).get("genres", []) for p in fetched}

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


def _norm(v: list[float]) -> float:
    return sum(x * x for x in v) ** 0.5


def blend_vectors(own: list[float] | None, other: list[float] | None, other_weight: float) -> list[float] | None:
    """Mix two taste vectors after normalising both, so neither dominates by magnitude."""
    if own is None:
        return other
    if other is None or other_weight <= 0:
        return own
    a, b = _norm(own) or 1.0, _norm(other) or 1.0
    return [(1 - other_weight) * x / a + other_weight * y / b for x, y in zip(own, other)]


def _taste(qdrant: QdrantClient, user_id: str, bucket: str) -> list[float] | None:
    pts = qdrant.retrieve(collection_name="taste_vectors", ids=[taste_point_id(user_id, bucket)], with_vectors=True)
    return list(pts[0].vector) if pts else None


def resolve_taste_vector(
    qdrant: QdrantClient, user_id: str, item_type: str, taste_from: str | None = None, blend: float = 0.0
) -> tuple[list[float] | None, str]:
    """The vector to search with, and a label for how it was built (goes into the response).

    taste_from: use another domain's taste (e.g. music taste to find films).
    blend: 0-1 share of the cross-domain (_combined) taste mixed into the domain's own.
    """
    if taste_from:
        vec = _taste(qdrant, user_id, taste_from)
        if vec is not None:
            own = _taste(qdrant, user_id, item_type) if blend > 0 else None
            return (blend_vectors(vec, own, blend) if own else vec), f"from:{taste_from}"
    own = _taste(qdrant, user_id, item_type)
    combined = _taste(qdrant, user_id, "_combined") if (own is None or blend > 0) else None
    if own is None:
        # No item_type-specific taste yet — fall back to the combined cross-domain vector.
        return combined, "combined" if combined is not None else "none"
    if combined is not None and blend > 0:
        return blend_vectors(own, combined, blend), f"own+combined:{blend:g}"
    return own, "own"


async def generate_candidates(
    pool: asyncpg.Pool,
    qdrant: QdrantClient,
    user_id: str,
    item_type: str,
    total_sources: int = 7,
    *,
    group: list[uuid.UUID] | None = None,
    taste_from: str | None = None,
    blend: float = 0.0,
    sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
) -> list[Candidate]:
    """user_id is the canonical id; group every id of the same person (defaults to [user_id])."""
    group = group or [uuid.UUID(user_id)]
    taste, _ = resolve_taste_vector(qdrant, user_id, item_type, taste_from, blend)
    if taste is None:
        return []  # cold-start: no taste vector at all yet, nothing to rank against

    owned = await get_owned_item_ids(pool, group)
    genre_stats = await get_user_genre_stats(pool, qdrant, group)

    must = [FieldCondition(key="item_type", match=MatchValue(value=item_type))]
    if sources:
        must.append(FieldCondition(key="source", match=MatchAny(any=sources)))
    must_not = [FieldCondition(key="source", match=MatchAny(any=exclude_sources))] if exclude_sources else None
    hits = qdrant.query_points(
        collection_name="content_items",
        query=taste,
        query_filter=Filter(must=must, must_not=must_not),
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
                title=payload.get("title", ""),
            )
        )
    return candidates
