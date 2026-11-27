"""Queue outgoing notifications (webhooks) from any service; services/notifier delivers them.

Household hooks (webhook.user_id NULL) receive every event; a person's hooks receive only
events about them. `key` makes an announcement idempotent (a new release is announced once).
"""

from __future__ import annotations

import uuid
from typing import Any

EVENTS = ("new_release", "wanted", "digest", "question", "goal", "recommendation", "group", "context", "admin")


async def emit(conn: Any, event: str, payload: dict, user_id: uuid.UUID | None = None, key: str | None = None) -> int:
    if key:
        fresh = await conn.fetchval(
            "INSERT INTO notification_log (key) VALUES ($1) ON CONFLICT DO NOTHING RETURNING key", key
        )
        if fresh is None:
            return 0
    hooks = await conn.fetch(
        """
        SELECT id FROM webhook
        WHERE active AND ($1 = ANY(events) OR '*' = ANY(events))
          AND (user_id IS NULL OR user_id = $2)
        """,
        event, user_id,
    )
    body = {"event": event, "user_id": str(user_id) if user_id else None, **payload}
    for h in hooks:
        await conn.execute(
            "INSERT INTO webhook_delivery (webhook_id, event, payload) VALUES ($1, $2, ($3::text)::jsonb)",  # text: works with or without a jsonb codec
            h["id"], event, _json(body),
        )
    return len(hooks)


def _json(v: Any) -> str:
    import json

    return json.dumps(v, default=str)


# --- the admin's feed (Vesper for video, Mono for music) ----------------------------------------

ADMIN_EVENT = "admin"  # webhook event name for everything that lands in the admin feed


async def admin(
    conn: Any,
    kind: str,
    domain: str,
    title: str,
    body: str,
    *,
    severity: str = "info",
    item_type: str | None = None,
    source_item_id: str | None = None,
    extra: dict | None = None,
    actions: list[str] | None = None,
    key: str | None = None,
) -> str | None:
    """Add one entry to the admin's notification feed and queue it for household webhooks.

    `domain` decides where it shows: 'video' in Vesper, 'music' in Mono, 'system' in both.
    `actions` are what a client may offer (e.g. ['keep', 'remove_now']); the query API
    performs them (POST /v1/admin/notifications/{id}/actions/{action}). `key` makes it
    idempotent. The title and body are sealed at rest. Returns the id, or None if `key`
    was already used.
    """
    from .crypto import P_ADMIN_NOTE, get_vault

    data = {"title": title, "body": body, "actions": actions or [], **(extra or {})}
    nid = await conn.fetchval(
        """INSERT INTO admin_notification (domain, kind, severity, item_type, source_item_id, data, dedupe_key)
           VALUES ($1, $2, $3, $4, $5, ($6::text)::jsonb, $7) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
        domain, kind, severity, item_type, source_item_id, _json(get_vault().seal_json(data, P_ADMIN_NOTE)), key,
    )
    if nid is None:
        return None
    hooks = await conn.fetch(
        "SELECT id FROM webhook WHERE active AND user_id IS NULL AND ($1 = ANY(events) OR '*' = ANY(events))", ADMIN_EVENT,
    )
    body_json = _json({"event": ADMIN_EVENT, "id": str(nid), "domain": domain, "kind": kind, "severity": severity,
                       "item_type": item_type, "source_item_id": source_item_id, **data})
    for h in hooks:
        await conn.execute(
            "INSERT INTO webhook_delivery (webhook_id, event, payload) VALUES ($1, $2, ($3::text)::jsonb)", h["id"], ADMIN_EVENT, body_json,
        )
    return str(nid)
