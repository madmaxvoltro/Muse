import json
import logging
import uuid

from fastapi import Depends, FastAPI
from muse_schema import DEFAULT_ACTION_WEIGHTS, Event
from pydantic import BaseModel, Field

from .auth import require_adapter_key
from .db import close_pool, get_pool
from .identity import canonical
from .ratelimit import check_rate_limit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.ingestion")

app = FastAPI(title="Muse Ingestion API", version="0.2.0")

INSERT_SQL = """
INSERT INTO event (
    user_id, source, source_item_id, item_type, action, action_weight,
    "timestamp", duration_ms, progress_pct, metadata, device_context
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
"""

# With metadata.dedupe_key the insert is idempotent per (user, source, key): pollers can
# re-send after a restart (or import history) without double counting.
INSERT_DEDUPED_SQL = """
INSERT INTO event (
    user_id, source, source_item_id, item_type, action, action_weight,
    "timestamp", duration_ms, progress_pct, metadata, device_context
)
SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM event WHERE user_id = $1 AND source = $2 AND metadata->>'dedupe_key' = $12
)
"""

BATCH_MAX = 500


class EventBatch(BaseModel):
    events: list[Event] = Field(..., min_length=1, max_length=BATCH_MAX)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _insert(conn, event: Event, source: str) -> None:
    """The key decides `source` server-side: an adapter cannot claim to be another source,
    whatever the body says. Linked identities are resolved here, so every reader sees one
    person's events under one user_id; the origin is kept in metadata.original_user_id.
    """
    if event.source.value != source:
        event.source = event.source.__class__(source)  # server-side source wins

    metadata = dict(event.metadata)
    user_id = await canonical(conn, event.user_id)
    if user_id != event.user_id:
        metadata["original_user_id"] = str(event.user_id)
    if isinstance(metadata.get("together"), list):
        together = []
        for raw in metadata["together"]:
            try:
                together.append(str(await canonical(conn, uuid.UUID(str(raw)))))
            except ValueError:
                continue
        metadata["together"] = sorted(set(together) - {str(user_id)})

    weight = event.action_weight if event.action_weight is not None else DEFAULT_ACTION_WEIGHTS[event.action]
    dedupe = metadata.get("dedupe_key")
    args = [] if dedupe is None else [str(dedupe)]
    await conn.execute(
        INSERT_SQL if dedupe is None else INSERT_DEDUPED_SQL,
        user_id,
        event.source.value,
        event.source_item_id,
        event.item_type.value,
        event.action.value,
        weight,
        event.timestamp,
        event.duration_ms,
        event.progress_pct,
        json.dumps(metadata),
        json.dumps(event.device_context) if event.device_context is not None else None,
        *args,
    )


@app.post("/events", status_code=202)
async def ingest_event(event: Event, source: str = Depends(require_adapter_key)) -> dict[str, str]:
    """Adapters POST here, authenticated with their own X-Muse-Api-Key."""
    check_rate_limit(source)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _insert(conn, event, source)
    logger.info("ingested event source=%s action=%s user=%s", source, event.action.value, event.user_id)
    return {"status": "accepted"}


@app.post("/events/batch", status_code=202)
async def ingest_batch(batch: EventBatch, source: str = Depends(require_adapter_key)) -> dict:
    """Many events in one request (impressions for a whole home screen, a history import).
    Counts against the rate limit per 25 events, and is all-or-nothing.
    """
    check_rate_limit(source, cost=max(1, len(batch.events) // 25))
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for event in batch.events:
                await _insert(conn, event, source)
    logger.info("ingested batch source=%s n=%d", source, len(batch.events))
    return {"status": "accepted", "count": len(batch.events)}


