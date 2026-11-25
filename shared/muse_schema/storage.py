"""How an event is stored. Every writer (ingestion API, the recommendation API's Soon Gone
whitelist, tests) goes through `event_row`, so the encrypted form is identical everywhere:

  source_item_id -> blind index (joins with item.item_key = source || ':' || source_item_id)
  metadata       -> sealed, with the real item id inside as `_sid`
  dedupe_key     -> blind index, kept outside the seal so idempotent inserts still work
  device_context -> sealed
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .crypto import P_DEDUPE, P_EVENT_METADATA, event_item_bid

INSERT_SQL = """
INSERT INTO event (
    user_id, source, source_item_id, item_type, action, action_weight,
    "timestamp", duration_ms, progress_pct, metadata, device_context
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
"""

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


def event_row(
    vault: Any,
    *,
    user_id: Any,
    source: str,
    source_item_id: str | None,
    item_type: str,
    action: str,
    action_weight: float,
    timestamp: datetime,
    duration_ms: int | None = None,
    progress_pct: float | None = None,
    metadata: dict | None = None,
    device_context: dict | None = None,
) -> tuple[list, str | None]:
    """-> (11 positional args for INSERT_SQL with metadata/device_context as dicts, blind dedupe key or None)."""
    md = dict(metadata or {})
    dedupe = md.pop("dedupe_key", None)
    if source_item_id is not None:
        md["_sid"] = source_item_id
    blind_dedupe = vault.bid(f"{source}:{dedupe}", P_DEDUPE) if dedupe is not None else None
    stored_md: dict = vault.seal_json(md, P_EVENT_METADATA) if (md and vault.enabled) else md
    if blind_dedupe is not None:
        stored_md = {**stored_md, "dedupe_key": blind_dedupe}
    dc = vault.seal_json(device_context, P_EVENT_METADATA) if device_context is not None and vault.enabled else device_context
    args = [user_id, source, event_item_bid(vault, source, source_item_id), item_type, action, action_weight,
            timestamp, duration_ms, progress_pct, stored_md, dc]
    return args, blind_dedupe
