import json
import logging

from fastapi import Depends, FastAPI
from muse_schema import DEFAULT_ACTION_WEIGHTS, Event

from .auth import require_adapter_key
from .db import close_pool, get_pool
from .ratelimit import check_rate_limit

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.ingestion")

app = FastAPI(title="Muse Ingestion API", version="0.1.0")

INSERT_SQL = """
INSERT INTO event (
    user_id, source, source_item_id, item_type, action, action_weight,
    "timestamp", duration_ms, progress_pct, metadata, device_context
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
"""


@app.on_event("shutdown")
async def _shutdown() -> None:
    await close_pool()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events", status_code=202)
async def ingest_event(event: Event, source: str = Depends(require_adapter_key)) -> dict[str, str]:
    """Adapters POST here, authenticated with their own X-Muse-Api-Key.

    The key determines `source` server-side — an adapter cannot claim to be a
    different source than the one it was issued a key for, even if it lies in
    the request body's `source` field. This is intentional defense against a
    compromised adapter spoofing another source's events.
    """
    if event.source.value != source:
        event.source = event.source.__class__(source)  # server-side source wins

    check_rate_limit(source)

    weight = event.action_weight if event.action_weight is not None else DEFAULT_ACTION_WEIGHTS[event.action]

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            INSERT_SQL,
            event.user_id,
            event.source.value,
            event.source_item_id,
            event.item_type.value,
            event.action.value,
            weight,
            event.timestamp,
            event.duration_ms,
            event.progress_pct,
            json.dumps(event.metadata),
            json.dumps(event.device_context) if event.device_context is not None else None,
        )

    logger.info("ingested event source=%s action=%s user=%s", source, event.action.value, event.user_id)
    return {"status": "accepted"}
