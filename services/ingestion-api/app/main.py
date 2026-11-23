import json
import logging
import time
from datetime import datetime, timezone

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


_incognito: dict[str, tuple[float, bool]] = {}


async def incognito(conn, user_id) -> bool:
    """Is this person (via any linked id) in incognito mode? Cached for 30 s."""
    key = str(user_id)
    hit = _incognito.get(key)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    try:
        until = await conn.fetchval(
            "SELECT o.preferences->>'incognito_until' FROM user_overlay o "
            "WHERE o.user_id = COALESCE((SELECT user_id FROM user_alias WHERE alias_id = $1), $1)", user_id)
    except Exception:  # database without migration 007
        until = None
    on = bool(until) and datetime.fromisoformat(until) > datetime.now(timezone.utc)
    _incognito[key] = (time.monotonic(), on)
    return on


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
        if event.action.value != "cataloged" and await incognito(conn, event.user_id):
            return {"status": "dropped", "reason": "incognito"}
        dedupe = event.metadata.get("dedupe_key")
        args = [] if dedupe is None else [str(dedupe)]
        await conn.execute(
            INSERT_SQL if dedupe is None else INSERT_DEDUPED_SQL,
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
            *args,
        )

    logger.info("ingested event source=%s action=%s user=%s", source, event.action.value, event.user_id)
    return {"status": "accepted"}
