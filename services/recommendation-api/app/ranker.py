"""Learned ranking weights (trained hourly by the embedding worker from impressions and what
came of them). Used only when the trained model beat the hand weights on held-out data."""

from __future__ import annotations

import json
import time

from .engine import WEIGHTS

_cache: dict[str, tuple[float, dict | None]] = {}
TTL = 600


async def weights_for(conn, item_type: str) -> tuple[dict[str, float], str]:
    hit = _cache.get(item_type)
    if hit and time.monotonic() - hit[0] < TTL:
        return (hit[1] or WEIGHTS), ("learned" if hit[1] else "hand")
    learned = None
    try:
        row = await conn.fetchrow(
            "SELECT weights FROM engine_ranker_model WHERE active AND item_type IN ($1, '_all') ORDER BY (item_type = $1) DESC, trained_at DESC LIMIT 1",
            item_type)
        if row:
            w = row["weights"]
            w = json.loads(w) if isinstance(w, str) else w
            learned = {k: float(v) for k, v in w.items() if k in WEIGHTS}
    except Exception:
        learned = None
    _cache[item_type] = (time.monotonic(), learned)
    return (learned or WEIGHTS), ("learned" if learned else "hand")
