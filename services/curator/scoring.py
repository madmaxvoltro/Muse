"""Scores discovery candidates and owned items against active users' taste vectors.
Multi-profile combination rule (decided, see docs/architecture.md):
  - ADD uses the averaged similarity across active profiles.
  - REMOVE requires a low score from ALL active profiles (max-score-wins for keeping).
"""

from muse_schema import taste_point_id
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

CONTENT_COLLECTION = "content_items"
TASTE_COLLECTION = "taste_vectors"
REMOVE_SCORE_THRESHOLD = 0.2  # below this similarity for every active profile -> removal-eligible


def embed_text(model: SentenceTransformer, text: str) -> list[float]:
    return model.encode(text, normalize_embeddings=True).tolist()


def _taste_vectors_for(qdrant: QdrantClient, user_ids: list[str], item_type: str) -> list[list[float]]:
    vectors = []
    for user_id in user_ids:
        points = qdrant.retrieve(collection_name=TASTE_COLLECTION, ids=[taste_point_id(user_id, item_type)], with_vectors=True)
        if not points:
            points = qdrant.retrieve(collection_name=TASTE_COLLECTION, ids=[taste_point_id(user_id, "_combined")], with_vectors=True)
        if points:
            vectors.append(points[0].vector)
    return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def score_candidate_avg(qdrant: QdrantClient, candidate_vector: list[float], active_user_ids: list[str], item_type: str) -> float:
    """ADD decision: averaged similarity across active profiles."""
    tastes = _taste_vectors_for(qdrant, active_user_ids, item_type)
    if not tastes:
        return 0.0
    scores = [_cosine(candidate_vector, t) for t in tastes]
    return sum(scores) / len(scores)


def score_owned_item_max(qdrant: QdrantClient, source: str, source_item_id: str, active_user_ids: list[str], item_type: str) -> float | None:
    """REMOVE decision input: the MAX similarity across active profiles — an item is
    only removal-eligible if even its best-matching profile scores it low.
    Returns None if the item has no content embedding yet (can't score it).
    """
    from muse_schema import content_point_id

    points = qdrant.retrieve(collection_name=CONTENT_COLLECTION, ids=[content_point_id(source, source_item_id)], with_vectors=True)
    if not points:
        return None
    vector = points[0].vector
    tastes = _taste_vectors_for(qdrant, active_user_ids, item_type)
    if not tastes:
        return None
    return max(_cosine(vector, t) for t in tastes)
