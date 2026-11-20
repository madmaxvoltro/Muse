"""Minimal fixed-window rate limiter per adapter source, in-memory.
Good enough for a single-instance ingestion API; swap for Redis if this ever scales out.
Protects against a misbehaving/compromised adapter flooding the event store.
"""

import os
import time
from collections import defaultdict

from fastapi import HTTPException

_WINDOW_SECONDS = 60
_MAX_EVENTS_PER_WINDOW = int(os.environ.get("MUSE_RATE_LIMIT_PER_MINUTE", "1200"))

_hits: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(source: str, cost: int = 1) -> None:
    now = time.monotonic()
    window_start = now - _WINDOW_SECONDS
    hits = [t for t in _hits[source] if t > window_start]
    if len(hits) + cost > _MAX_EVENTS_PER_WINDOW:
        raise HTTPException(status_code=429, detail=f"rate limit exceeded for source={source}")
    hits.extend([now] * cost)
    _hits[source] = hits
