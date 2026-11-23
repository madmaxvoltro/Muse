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


def open_event(row: Any, vault: Any = None) -> dict:
    """An event row as its writer sent it: the real source_item_id (from the sealed `_sid`),
    opened metadata (without `_sid` and the blind dedupe key) and device_context. Works on
    plaintext rows too (MUSE_ENCRYPTION=off, or rows written before encryption), so readers
    never need to know which they got. Readers that filter or group by item must do it on
    the stored (blind) source_item_id, which is consistent per item."""
    from .crypto import get_vault

    vault = vault or get_vault()
    d = dict(row)
    md = d.get("metadata")
    if md is not None:
        if isinstance(md, str):
            import json

            try:
                md = json.loads(md)
            except ValueError:
                md = {}
        if isinstance(md, dict):
            md = {k: v for k, v in md.items() if k != "dedupe_key"}  # blind index, kept outside the seal
        opened = vault.open_json(md, P_EVENT_METADATA) if md else {}
        if not isinstance(opened, dict):
            opened = {}
        opened = {k: v for k, v in opened.items() if k != "dedupe_key"}
        sid = opened.pop("_sid", None)
        if sid is not None:
            d["blind_item_id"] = d.get("source_item_id")
            d["source_item_id"] = sid
        d["metadata"] = opened
    dc = d.get("device_context")
    if dc is not None:
        if isinstance(dc, str):
            import json

            try:
                dc = json.loads(dc)
            except ValueError:
                dc = None
        d["device_context"] = vault.open_json(dc, P_EVENT_METADATA) if dc is not None else None
    return d


async def fetch_open(conn: Any, sql: str, *args: Any) -> list[dict]:
    """conn.fetch + open_event on every row. The query must select `metadata` (where the real
    item id lives when encrypted) for rows whose source_item_id the caller uses."""
    return [open_event(r) for r in await conn.fetch(sql, *args)]


def item_bid(source: str, source_item_id: str | None) -> str | None:
    """The stored (blind) form of a real item id, for WHERE source_item_id = ... and joins."""
    from .crypto import event_item_bid, get_vault

    return event_item_bid(get_vault(), source, source_item_id)


def blind_keys(keys: list[tuple[str, str]]) -> tuple[list[str], list[str], dict[tuple[str, str], tuple[str, str]]]:
    """For `JOIN unnest($sources, $ids)` lookups by real (source, id) pairs: the arrays to pass
    and a map from the stored pair back to the real one. Each key is looked up by its blind id
    and by its plain id, so rows written before encryption was turned on (until `muse admin`
    has sealed them) are still found."""
    sources, bids, back = [], [], {}
    for s, sid in keys:
        for stored in {item_bid(s, sid), sid}:
            sources.append(s)
            bids.append(stored)
            back[(s, stored)] = (s, sid)
    return sources, bids, back


async def insert_event(conn: Any, *, user_id: Any, source: str, source_item_id: str | None, item_type: str, action: str,
                       action_weight: float, timestamp: datetime | None = None, duration_ms: int | None = None,
                       progress_pct: float | None = None, metadata: dict | None = None,
                       device_context: dict | None = None) -> bool:
    """Write one event the way the ingestion API does (encrypted, idempotent with
    metadata.dedupe_key). For services that write events themselves (the query API's ratings and
    answers, imports, the catalog). False when the dedupe key was already there."""
    from datetime import timezone

    from .crypto import get_vault

    args, dedupe = event_row(get_vault(), user_id=user_id, source=source, source_item_id=source_item_id, item_type=item_type,
                             action=action, action_weight=action_weight, timestamp=timestamp or datetime.now(timezone.utc),
                             duration_ms=duration_ms, progress_pct=progress_pct, metadata=metadata, device_context=device_context)
    import json

    # Sent as text, so it works whether or not the connection has a jsonb codec.
    args[9] = json.dumps(args[9]) if args[9] is not None else None
    args[10] = json.dumps(args[10]) if args[10] is not None else None
    as_text = lambda sql: sql.replace("$10::jsonb", "$10::text::jsonb").replace("$11::jsonb", "$11::text::jsonb")  # noqa: E731
    if dedupe is None:
        status = await conn.execute(as_text(INSERT_SQL), *args)
    else:
        status = await conn.execute(as_text(INSERT_DEDUPED_SQL), *args, dedupe)
    return status.split()[-1] != "0"


def item_ids(source: str, source_item_id: str) -> list[str]:
    """Both stored forms of one item id, for `WHERE source_item_id = ANY($n::text[])`: the blind
    id, and the plain one for rows written before encryption was turned on."""
    return list(dict.fromkeys([item_bid(source, source_item_id), source_item_id]))
