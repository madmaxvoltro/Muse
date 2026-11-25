"""Minimal fixed-window rate limiter per adapter source, in-memory.
Good enough for a single-instance ingestion API; swap for Redis if this ever scales out.
Protects against a misbehaving/compromised adapter flooding the event store. History imports
are bulk by nature and get a much larger budget (they're user-initiated uploads).
"""

import time
from collections import defaultdict

from fastapi import HTTPException

_WINDOW_SECONDS = 60
_MAX_EVENTS_PER_WINDOW = 300
_LIMITS = {"import": 200_000, "muse": 2_000}

_hits: dict[str, list[tuple[float, int]]] = defaultdict(list)


def check_rate_limit(source: str, n: int = 1) -> None:
    now = time.monotonic()
    window_start = now - _WINDOW_SECONDS
    hits = [(t, c) for t, c in _hits[source] if t > window_start]
    limit = _LIMITS.get(source, _MAX_EVENTS_PER_WINDOW)
    if sum(c for _, c in hits) + n > limit:
        raise HTTPException(status_code=429, detail=f"rate limit exceeded for source={source}")
    hits.append((now, n))
    _hits[source] = hits
