"""Names and payload shapes shared by every service that touches the vector store, so the
worker (writer) and the recommendation API / curator (readers) can't drift apart.

Encryption at rest (muse_schema.crypto): the Qdrant payload keeps in plaintext only what the
vector search must filter or order on (item type, domain, availability, a quality score
rounded to one decimal, the day a title was added) plus the *blind* canonical key. Everything
that says what a title is (title, year, tags, collection, library ids, vote counts, the real
TMDB key) is one sealed blob. Point ids are derived from blind keys, so they can't be
precomputed from public TMDB ids either.
"""

from __future__ import annotations

from typing import Any

from muse_schema.crypto import P_CANON, P_CANON_DATA, P_PAYLOAD, get_vault

ITEMS_COLLECTION = "muse_items"  # one point per canonical title, id = muse_schema.canon_point_id(blind key)

# Payload fields the API filters / orders on; the worker creates payload indexes for them.
KEYWORD_INDEXES = ("item_type", "domain", "ck")
BOOL_INDEXES = ("available",)
FLOAT_INDEXES = ("quality",)


def blind_canon(real_or_blind: str | None) -> str | None:
    """The stored (blind) form of a canonical key. Idempotent."""
    return get_vault().bid(real_or_blind, P_CANON)


def payload_for(row: dict[str, Any]) -> dict[str, Any]:
    """canon_item row (with `data` opened) -> Qdrant payload (sealed)."""
    from .ranking import bayesian_rating

    d = row.get("data") or {}
    q = bayesian_rating(d.get("vote_average"), d.get("vote_count"))
    added = row.get("added_at")
    added_ts = added.timestamp() if hasattr(added, "timestamp") else added
    sealed = {
        "real_key": d.get("real_key"),
        "title": d.get("title") or "",
        "year": d.get("year"),
        "tags": list(d.get("tags") or []),
        "collection_id": d.get("collection_id"),
        "vote_average": d.get("vote_average"),
        "vote_count": d.get("vote_count"),
        "library_refs": d.get("library_refs") or [],
        "certification": d.get("certification"),
    }
    return {
        "ck": row["canon_key"],
        "item_type": row["item_type"],
        "domain": row["domain"],
        "available": bool(row.get("available")),
        "quality": round(float(q), 1) if q is not None else 0.0,
        "added_day": int(added_ts // 86400) if isinstance(added_ts, (int, float)) else None,
        "enc": get_vault().seal(sealed, P_PAYLOAD),
    }


def open_payload(pl: dict[str, Any] | None) -> dict[str, Any]:
    """Qdrant payload -> the flat dict the engine works with. `canon_key` is the blind key
    (what profiles, impressions and answers use); `real_key` the real one (tmdb:movie:603)."""
    pl = pl or {}
    inner = get_vault().open(pl.get("enc"), P_PAYLOAD) or {}
    added_day = pl.get("added_day")
    return {
        **inner,
        "canon_key": pl.get("ck"),
        "item_type": pl.get("item_type"),
        "domain": pl.get("domain"),
        "available": bool(pl.get("available")),
        "quality": pl.get("quality"),
        "added_at": added_day * 86400.0 if isinstance(added_day, (int, float)) else None,
    }


def seal_canon_data(data: dict[str, Any]) -> dict:
    return get_vault().seal_json(data, P_CANON_DATA)


def open_canon_data(value: Any) -> dict[str, Any]:
    return get_vault().open_json(value, P_CANON_DATA) or {}


def payload_hash(payload: dict[str, Any]) -> str:
    """Change detection on the *plaintext* payload content (sealed blobs differ every time)."""
    import json

    inner = get_vault().open(payload.get("enc"), P_PAYLOAD)
    body = {**{k: v for k, v in payload.items() if k != "enc"}, "inner": inner}
    return get_vault().digest(json.dumps(body, sort_keys=True, default=str), "qdrant.payload_hash")
