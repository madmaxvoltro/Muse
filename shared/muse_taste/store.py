"""Names and payload shapes shared by every service that touches the vector store, so the
worker (writer) and the recommendation API / curator (readers) can't drift apart."""

from __future__ import annotations

import hashlib
import json
from typing import Any

ITEMS_COLLECTION = "muse_items"  # one point per canonical title, id = muse_schema.canon_point_id

# Payload fields the API filters / orders on; the worker creates payload indexes for them.
KEYWORD_INDEXES = ("item_type", "domain", "canon_key", "collection_id")
BOOL_INDEXES = ("available",)
FLOAT_INDEXES = ("vote_count", "quality")


def payload_for(row: dict[str, Any]) -> dict[str, Any]:
    """canon_item row -> Qdrant payload."""
    from .ranking import bayesian_rating

    q = bayesian_rating(row.get("vote_average"), row.get("vote_count"))
    added = row.get("added_at")
    return {
        "canon_key": row["canon_key"],
        "item_type": row["item_type"],
        "domain": row["domain"],
        "title": row.get("title") or "",
        "year": row.get("year"),
        "tags": list(row.get("tags") or []),
        "collection_id": row.get("collection_id"),
        "vote_average": row.get("vote_average"),
        "vote_count": float(row.get("vote_count") or 0.0),
        "popularity": row.get("popularity"),
        "quality": float(q) if q is not None else 0.0,
        "available": bool(row.get("available")),
        "library_refs": row.get("library_refs") or [],
        "added_at": added.timestamp() if hasattr(added, "timestamp") else added,
    }


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]
