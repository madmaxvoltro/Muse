"""From raw events to one judgement per title.

The old pipeline averaged every event's `action_weight`. That made the taste profile measure
*how many events an adapter happened to emit*, not how much someone liked something:
a 2-hour film polled every 5 minutes produced ~24 `played` events (7 weight) and a 60-episode
binge 60 `completed` events, so long content and one binge drowned everything else.

Here every title (episodes rolled up into their series) is reduced to one `ItemSignal`:

* **preference**: signed, roughly -1.5 to 2.5: how much this person likes it.
* **confidence**: how sure we are, as evidence mass. This follows the split between
  preference and confidence in Hu, Koren & Volinsky, "Collaborative Filtering for Implicit
  Feedback Datasets" (ICDM 2008): watching to the end is strong evidence, a detail-page view
  weak, an explicit thumbs strongest.
* **weight(now)**: preference x time decay with a *real* half-life. Explicit judgements decay
  slower and never fully vanish.

Explicit beats implicit: the latest thumbs (or "not for me") overrides what viewing suggests.
Implicit negatives are inferred carefully: a film stopped at 12% and not resumed for two weeks
is an *abandon* (mild negative), unless the person said "not in the mood", which cancels it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# --- tunables (env-overridable through muse_taste.config in the services) -----------------
IMPLICIT_HALF_LIFE_DAYS = 120.0
EXPLICIT_HALF_LIFE_DAYS = 540.0
EXPLICIT_DECAY_FLOOR = 0.35  # a "love" from years ago still says something about you
ABANDON_AFTER_DAYS = 14.0
ABANDON_MAX_PROGRESS = 60.0
COMPLETE_PROGRESS = 85.0  # credits roll; Jellyfin often never marks these "played"
MUSIC_SKIP_PROGRESS = 30.0

RATING_PREF = {-1: -1.5, 1: 1.0, 2: 2.0}

ABANDON_REASON_PREF: dict[str, float | None] = {
    "not_for_me": -1.3,  # explicit dislike
    "too_slow": -0.6,
    "too_intense": -0.4,
    "bad_quality": 0.0,  # the file/stream, not the title
    "not_in_mood": None,  # cancels the implicit abandon penalty, nothing else
    "will_finish_later": None,
    "watched_elsewhere": 1.0,  # counts as completed
}

INTENT_ACTIONS = {
    "added_watchlist": 0.45,
    "requested": 0.55,
    "trailer_played": 0.15,
    "detail_viewed": 0.05,
    "searched": 0.2,
}


@dataclass
class ItemSignal:
    key: str
    item_type: str
    preference: float = 0.0
    confidence: float = 0.0
    last_ts: datetime | None = None
    explicit: bool = False
    seen: bool = False  # consumed or judged: don't recommend it as something new
    in_progress: bool = False
    abandoned: bool = False  # inferred, and nobody told us why yet
    intent: float = 0.0  # watchlist/request/trailer: want-to-watch, not yet watched
    sources: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)  # human-readable, for the profile page

    def decay(self, now: datetime) -> float:
        if self.last_ts is None:
            return 0.0
        age = max((now - self.last_ts).total_seconds() / 86400.0, 0.0)
        if self.explicit:
            return max(0.5 ** (age / EXPLICIT_HALF_LIFE_DAYS), EXPLICIT_DECAY_FLOOR)
        return 0.5 ** (age / IMPLICIT_HALF_LIFE_DAYS)

    def weight(self, now: datetime) -> float:
        return self.preference * self.decay(now)


@dataclass
class SignalEvent:
    """The event fields signals need, with the key already canonicalised (episodes rolled up
    to their series by the caller)."""

    key: str
    item_type: str
    action: str
    action_weight: float
    timestamp: datetime
    source: str = ""
    progress_pct: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sub_key: str | None = None  # the episode within a series, for counting distinct episodes


def _rating_value(ev: SignalEvent) -> int:
    v = ev.metadata.get("value")
    if isinstance(v, (int, float)):
        return int(v)
    w = ev.action_weight
    if w <= -0.5:
        return -1
    if w >= 1.75:
        return 2
    if w >= 0.5:
        return 1
    return 0


def aggregate(
    events: Iterable[SignalEvent],
    now: datetime | None = None,
    source_weights: dict[str, float] | None = None,
    disabled_sources: Iterable[str] = (),
) -> dict[str, ItemSignal]:
    """Reduces a user's events to one ItemSignal per key. `source_weights` and
    `disabled_sources` are the user overlay sliders (docs/architecture.md): they act here,
    on the read path, and never touch the stored events."""
    now = now or datetime.now(timezone.utc)
    disabled = set(disabled_sources)
    sw = source_weights or {}
    by_key: dict[str, list[SignalEvent]] = defaultdict(list)
    for ev in events:
        if ev.source in disabled or ev.action == "cataloged":
            continue
        by_key[ev.key].append(ev)

    out: dict[str, ItemSignal] = {}
    for key, evs in by_key.items():
        evs.sort(key=lambda e: e.timestamp)
        sig = _aggregate_item(key, evs, now)
        if sig is None:
            continue
        scale = max((sw.get(s, 1.0) for s in sig.sources), default=1.0)
        sig.preference *= scale
        out[key] = sig
    return out


def _aggregate_item(key: str, evs: list[SignalEvent], now: datetime) -> ItemSignal | None:
    item_type = evs[-1].item_type if evs[-1].item_type != "series_episode" else "series"
    if any(e.item_type == "series_episode" for e in evs):
        item_type = "series"
    sig = ItemSignal(key=key, item_type=item_type)
    sig.sources = {e.source for e in evs if e.source}
    sig.last_ts = evs[-1].timestamp

    rating: int | None = None
    rating_ts: datetime | None = None
    rejected = False
    favorited = False
    removed = False
    completions_days: set[str] = set()
    completed_eps: set[str] = set()
    started_eps: set[str] = set()
    max_progress = 0.0
    last_progress_ts: datetime | None = None
    plays = 0
    skips = 0
    chose = 0
    passed = 0
    reason: str | None = None
    intent = 0.0

    for e in evs:
        a = e.action
        if a == "rated":
            v = _rating_value(e)
            rating = v if v != 0 else None
            rating_ts = e.timestamp
        elif a == "rejected":
            rejected = True
        elif a == "favorited":
            favorited = True
        elif a == "removed":
            removed = True
        elif a == "completed":
            completions_days.add(e.timestamp.date().isoformat())
            if e.sub_key:
                completed_eps.add(e.sub_key)
                started_eps.add(e.sub_key)
            max_progress = 100.0
            last_progress_ts = e.timestamp
            plays += 1
        elif a == "played":
            p = e.progress_pct
            if e.sub_key:
                started_eps.add(e.sub_key)
                if p is not None and p >= COMPLETE_PROGRESS:
                    completed_eps.add(e.sub_key)
            if p is not None:
                if p > max_progress:
                    max_progress = p
                last_progress_ts = e.timestamp
            else:
                last_progress_ts = e.timestamp
            plays += 1
            if e.item_type == "track" and p is not None and p < MUSIC_SKIP_PROGRESS:
                skips += 1
        elif a == "skipped":
            skips += 1
        elif a == "chose":
            chose += 1
        elif a == "passed_over":
            passed += 1
        elif a == "abandon_reason":
            reason = str(e.metadata.get("reason") or "") or None
        elif a in INTENT_ACTIONS:
            intent = max(intent, INTENT_ACTIONS[a])

    # ---- explicit judgements win ----------------------------------------------------------
    if rating is not None:
        sig.preference = RATING_PREF[rating]
        sig.confidence = 3.0
        sig.explicit = True
        sig.seen = True
        sig.last_ts = rating_ts or sig.last_ts
        sig.evidence.append({-1: "thumbs down", 1: "thumbs up", 2: "loved it"}[rating])
        if favorited and rating > 0:
            sig.preference += 0.3
        return sig
    if reason in ABANDON_REASON_PREF and ABANDON_REASON_PREF[reason] is not None and ABANDON_REASON_PREF[reason] < 0:
        sig.preference = ABANDON_REASON_PREF[reason]  # type: ignore[assignment]
        sig.confidence = 2.5
        sig.explicit = True
        sig.seen = True
        sig.evidence.append(f"stopped: {reason.replace('_', ' ')}")
        return sig
    if rejected:
        sig.preference = -1.2
        sig.confidence = 2.5
        sig.explicit = True
        sig.seen = True
        sig.evidence.append("rejected")
        return sig

    # ---- implicit ---------------------------------------------------------------------
    pref = 0.0
    conf = 0.0
    is_series = item_type == "series"
    is_track = item_type == "track"
    idle_days = (now - last_progress_ts).total_seconds() / 86400.0 if last_progress_ts else 0.0

    if favorited:
        pref += 1.5
        conf += 2.0
        sig.explicit = True  # a favourite is a deliberate act; decays like one
        sig.seen = True
        sig.evidence.append("favourite")

    if reason == "watched_elsewhere":
        completions_days.add("elsewhere")
        max_progress = 100.0

    if is_series and (started_eps or completed_eps or completions_days):
        n_done = len(completed_eps) or len(completions_days)
        n_started = max(len(started_eps), n_done)
        if n_done:
            # Saturating: episode 1 says a lot, episode 40 adds little. 10+ episodes ~= a
            # completed film that was loved.
            pref += 1.4 * (1.0 - math.exp(-n_done / 4.0))
            conf += 1.0 + math.log1p(n_done)
            sig.evidence.append(f"{n_done} episode{'s' if n_done != 1 else ''} watched")
        if n_done <= 1 and n_started <= 2 and idle_days > 30 and reason not in ("not_in_mood", "will_finish_later"):
            pref -= 0.35  # sampled the pilot, dropped it
            conf += 0.5
            sig.abandoned = reason is None
            sig.evidence.append("dropped after the first episode")
        elif n_started and idle_days <= 45:
            sig.in_progress = True
        sig.seen = True
    elif is_track:
        listens = max(plays - skips, 0)
        if listens:
            pref += 0.9 * min(math.log1p(listens) / math.log1p(12), 1.0)
            conf += math.log1p(listens)
            sig.evidence.append(f"{listens} listen{'s' if listens != 1 else ''}")
        if skips:
            ratio = skips / max(plays, skips, 1)
            if ratio > 0.5 and listens < 3:
                pref -= 0.5 * ratio
                conf += 0.3 * skips
                sig.evidence.append("usually skipped")
        sig.seen = listens > 0
    else:
        n_complete = len(completions_days)
        if n_complete or max_progress >= COMPLETE_PROGRESS:
            n = max(n_complete, 1)
            pref += 1.0 + 0.35 * math.log2(n)  # rewatches are a strong tell
            conf += 2.0 + 0.5 * (n - 1)
            sig.seen = True
            sig.evidence.append("watched" if n == 1 else f"watched {n}x")
        elif max_progress > 2.0:
            if idle_days > ABANDON_AFTER_DAYS and max_progress < ABANDON_MAX_PROGRESS:
                if reason in ("not_in_mood", "will_finish_later", "bad_quality"):
                    sig.in_progress = reason == "will_finish_later"
                else:
                    # Quitting early is a clearer "no" than quitting at 55%.
                    pref += -0.5 * (1.0 - max_progress / ABANDON_MAX_PROGRESS) - 0.1
                    conf += 0.8
                    sig.abandoned = reason is None
                    sig.evidence.append(f"stopped at {max_progress:.0f}%")
                sig.seen = True
            else:
                pref += 0.25 + 0.5 * (max_progress / 100.0)
                conf += 0.5 + max_progress / 100.0
                sig.in_progress = max_progress < COMPLETE_PROGRESS
                sig.seen = max_progress >= 50.0
                sig.evidence.append(f"in progress ({max_progress:.0f}%)")

    if chose or passed:
        pref += 0.5 * chose - 0.12 * passed
        conf += 0.6 * (chose + passed)
        sig.evidence.append(f"picked {chose}x in this-or-that" if chose else "passed over in this-or-that")
    if removed and pref <= 0.5:
        pref -= 0.3
        conf += 0.3

    if intent > 0 and not sig.seen:
        sig.intent = intent
        pref += 0.4 * intent  # wanting to watch something is weak taste evidence too
        conf += 0.3
        sig.evidence.append("on your list" if intent >= 0.45 else "showed interest")

    if pref == 0.0 and conf == 0.0 and not sig.intent and not sig.seen and not sig.in_progress:
        return None
    if conf == 0.0:
        conf = 0.3  # touched, but neutral (e.g. "not in the mood"): known, not judged
    sig.preference = pref
    sig.confidence = conf
    return sig


def total_confidence(signals: dict[str, ItemSignal]) -> float:
    return sum(s.confidence for s in signals.values())


def from_rows(rows: Iterable[Any], vault: Any = None) -> list[SignalEvent]:
    """Event rows joined with their catalog item (columns: source, source_item_id, item_type,
    action, action_weight, timestamp, progress_pct, metadata, canon_key, parent_key,
    parent_canon) -> SignalEvents keyed by canonical title, episodes rolled up to their series.
    Shared by the worker and the recommendation API so both see the same keys."""
    from muse_schema.crypto import P_CANON, P_EVENT_METADATA, get_vault

    vault = vault or get_vault()
    out = []
    for r in rows:
        md = vault.open_json(r["metadata"], P_EVENT_METADATA) or {}  # sealed at rest
        own = r["canon_key"] or f"{r['source']}:{r['source_item_id']}"
        if r["item_type"] == "series_episode":
            key = r["parent_canon"] or r["parent_key"] or own
            sub = own
        else:
            # Muse's own events (answers) reference a canonical title; store-side keys are blind
            key = vault.bid(str(md["canon_key"]), P_CANON) if md.get("canon_key") else own
            sub = None
        out.append(
            SignalEvent(
                key=key,
                item_type=r["item_type"],
                action=r["action"],
                action_weight=float(r["action_weight"] or 0.0),
                timestamp=r["timestamp"],
                source=r["source"],
                progress_pct=r["progress_pct"],
                metadata=md,
                sub_key=sub,
            )
        )
    return out
