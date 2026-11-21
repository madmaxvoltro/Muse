"""Re-consumption: what's worth watching or hearing *again*.

Everything else in Muse excludes what you've seen. But people rewatch comfort films and replay
favourite songs all the time, and a good recommender suggests the right old favourite at the
right moment.

Memory model: ACT-R's base-level activation (Anderson & Lebiere, "The Atomic Components of
Thought", 1998), used for music relistening by Reiter-Haas et al., "Predicting Music Relistening
Behavior Using the ACT-R Framework" (RecSys 2021). Activation of an item is

    B = ln( sum_j  (t_now - t_j) ^ -d )        d = 0.5, t in days

high when the item was consumed recently and often. Recency and frequency (the base-level
component) were the strongest single predictors of relistening in that study. Repeat
consumption in general follows the same two drivers (Anderson et al., "The Dynamics of Repeat
Consumption", WWW 2014; Benson, Kumar & Tomkins, "Modeling User Consumption Sequences", WWW 2016).

How activation is used differs by medium, because the behaviour differs:

* **Music (relisten)**: high activation means it's on your mind, so you probably want it again.
  score = liking x sigmoid(B - bias).
* **Films/series (rewatch)**: a film rewatched last week is the *last* thing you want. The pull
  of a loved film grows as its memory fades: score = liking x (1 - sigmoid(B - bias)) x
  (1 + rewatch history bonus), and only for titles you clearly loved (thumbs, favourites,
  rewatches).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from .signals import ItemSignal, SignalEvent

DECAY = 0.5
CONSUME_ACTIONS = {"completed", "played"}
MUSIC_BIAS = -0.5  # activation around which relisten pull is half
VIDEO_BIAS = -2.2  # activation (roughly one watch ~3 months ago) around which rewatch pull is half
MIN_VIDEO_GAP_DAYS = 120
MIN_VIDEO_PREFERENCE = 1.4  # loved: thumbs-up+, favourite, or rewatched


def activation(times: list[datetime], now: datetime, d: float = DECAY) -> float:
    s = sum(max((now - t).total_seconds() / 86400.0, 1.0) ** (-d) for t in times)
    return math.log(s) if s > 0 else -10.0


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def consumption_times(events: list[SignalEvent]) -> dict[str, list[datetime]]:
    """One timestamp per title per day it was consumed."""
    seen: dict[str, set[str]] = defaultdict(set)
    out: dict[str, list[datetime]] = defaultdict(list)
    for e in events:
        if e.action not in CONSUME_ACTIONS:
            continue
        if e.action == "played" and e.item_type != "track" and (e.progress_pct or 0) < 50:
            continue
        day = e.timestamp.date().isoformat()
        if day not in seen[e.key]:
            seen[e.key].add(day)
            out[e.key].append(e.timestamp)
    return out


def candidates(domain: str, signals: dict[str, ItemSignal], events: list[SignalEvent], now: datetime | None = None, limit: int = 60) -> list[dict]:
    """-> [{key, score, activation, times, last, why}] best first."""
    now = now or datetime.now(timezone.utc)
    times = consumption_times(events)
    out = []
    for key, ts in times.items():
        s = signals.get(key)
        if s is None or s.preference <= 0:
            continue
        a = activation(ts, now)
        last = max(ts)
        gap = (now - last).total_seconds() / 86400.0
        if domain == "music":
            liking = min(s.preference, 2.0) / 2.0
            score = liking * _sig(a - MUSIC_BIAS)
            why = "on repeat lately" if a > MUSIC_BIAS else "a favourite you haven't played in a while"
        else:
            if s.preference < MIN_VIDEO_PREFERENCE or gap < MIN_VIDEO_GAP_DAYS or s.in_progress:
                continue
            liking = min(s.preference, 2.5) / 2.5
            faded = 1.0 - _sig(a - VIDEO_BIAS)
            bonus = 1.0 + 0.3 * math.log2(len(ts)) if len(ts) > 1 else 1.0
            score = liking * faded * bonus
            years = gap / 365.0
            why = f"you loved it, {years:.0f} year{'s' if round(years) != 1 else ''} ago" if years >= 1 else f"you loved it, {gap / 30:.0f} months ago"
        out.append({"key": key, "score": round(score, 4), "activation": round(a, 3), "times": len(ts), "last": last.date().isoformat(), "why": why})
    out.sort(key=lambda r: -r["score"])
    return out[:limit]
