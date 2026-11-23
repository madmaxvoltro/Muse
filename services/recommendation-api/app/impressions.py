"""Logging what was shown, so Muse can learn what people pick and what they ignore.

Items are stored by their blind id (muse_schema.storage.item_bid), like events: an impression
log is a list of what someone was shown, which is personal."""

from __future__ import annotations

import json
import logging
import uuid

from muse_schema.storage import item_bid

log = logging.getLogger("muse.rec.impressions")


async def log_impressions(pool, request_id: uuid.UUID, user_id: uuid.UUID, item_type: str, items, surface: str) -> None:
    rows = [
        (request_id, user_id, item_type, it.source, item_bid(it.source, it.source_item_id), pos, float(it.score), it.confidence < 0.5,
         ",".join(sorted(it.generators))[:200], surface, json.dumps({k: round(v, 5) for k, v in it.features.items()}))
        for pos, it in enumerate(items)
    ]
    if not rows:
        return
    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO engine_impression (request_id, user_id, item_type, source, source_item_id, position, score, explore, "
                "generator, surface, features, item_bid) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::text::jsonb, $5)", rows)
    except Exception as e:  # never fail a recommendation over logging
        log.info("impression logging skipped: %s", e)
