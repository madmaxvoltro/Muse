"""Deterministic Qdrant point IDs, shared by the embedding worker (which writes
points) and the recommendation API (which reads them) so both always agree on
where a given item/user-taste vector lives without a lookup table.
"""

import uuid

_NAMESPACE = uuid.UUID("6f1b1b4a-2f2a-4b7b-9b0a-6a2c1d9e7f00")


def content_point_id(source: str, source_item_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"content:{source}:{source_item_id}"))


def taste_point_id(user_id: str, item_type: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"taste:{user_id}:{item_type}"))
