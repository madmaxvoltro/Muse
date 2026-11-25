"""Queue outgoing notifications (webhooks) from any service; services/notifier delivers them.

Household hooks (webhook.user_id NULL) receive every event; a person's hooks receive only
events about them. `key` makes an announcement idempotent (a new release is announced once).
"""

from __future__ import annotations

import uuid
from typing import Any

EVENTS = ("new_release", "wanted", "digest", "question", "goal", "recommendation", "group", "context")


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
