"""Taste evolution: how your taste changes over time, and how Muse responds to that.

A profile is a snapshot. Taste moves: an interest emerges (you've just discovered Korean
thrillers), grows, levels off, fades, and sometimes comes back years later. In between you
can get *saturated* (twelve true-crime docs in two weeks, then the next three abandoned), and
there are rhythms (light comedy on weekday evenings, documentaries on Sunday mornings).
This module turns a person's timestamped history plus their current interests into:

* **Lifecycle per interest**: emerging / growing / stable / fading / dormant, from the ratio
  of a short-memory and a long-memory engagement rate. This is the time-changing-preference
  idea from Koren, "Collaborative Filtering with Temporal Dynamics" (KDD 2009), applied per
  interest; DIEN (Zhou et al., "Deep Interest Evolution Network", AAAI 2019) models the same
  evolving-interest process with a sequence model, which needs far more data than a household.
* **Satiation**: a leaky bucket of recent consumption per interest, calibrated to *your*
  normal pace for it, combined with signs of declining engagement (abandons, thumbs down).
  Intense consumption is followed by boredom that recovers with time (Kapoor et al., "Just in
  Time Recommendations: Modeling the Dynamics of Boredom in Activity Streams", WSDM 2015). A
  binge alone isn't boredom; a binge followed by abandons is.
* **Rediscovery**: interests that were once big and have been dormant for months come back
  in small doses. People return to what they loved, and the pull grows with time away
  (Anderson et al., "The Dynamics of Repeat Consumption", WWW 2014).
* **Rhythms**: per interest, lift by daypart, weekday/weekend and season, with Dirichlet
  smoothing toward your overall pattern so a handful of plays doesn't create a "rule".
* **Stretch** (broadening): the band just outside each interest's edge (its radius), the
  "adjacent possible". Explore slots prefer candidates there, so exploration is directed
  outward from what you like rather than random. How wide the band is adapts to how often you
  take such suggestions (Beta prior). Accuracy alone makes recommenders narrow; serendipity
  and coverage need explicit design (Ge, Delgado-Battenfeld & Jannach, "Beyond accuracy",
  RecSys 2010; Kotkov, Wang & Veijalainen, "A survey of serendipity in recommender systems",
  KBS 2016).
* **Timeline**: per month, the share of each interest, taste breadth (effective number of
  interests), drift (how far the month's centre moved), and milestones.

Everything here changes recommendations *subtly and explainably* (ranking.py features with
reasons, calibration targets in rerank.py), and `user_overlay.evolution_strength` scales or
disables it (0 = off, 1 = default, 2 = strong).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np

from .profile import TasteProfile, normalise_sim, unit
from .signals import SignalEvent

SHORT_HALF_LIFE_DAYS = 21.0
LONG_HALF_LIFE_DAYS = 120.0
SATIATION_HALF_LIFE_DAYS = 10.0
BINGE_FACTOR = 2.5  # bucket above 2.5x your steady state for that interest = intense
EMERGING_DAYS = 90
DORMANT_DAYS = 120
ASSIGN_THRESHOLD = 0.2  # normalised similarity needed to attribute a touch to an interest
MIN_TOUCHES_FOR_RHYTHM = 8
RHYTHM_PRIOR = 5.0  # Dirichlet pseudo-counts toward your overall context distribution

DAYPARTS = ("night", "morning", "afternoon", "evening")
WEEKPARTS = ("weekday", "weekend")
SEASONS = ("winter", "spring", "summer", "autumn")

TOUCH_WEIGHTS = {
    "completed": 1.0,
    "favorited": 1.0,
    "chose": 0.5,
    "rejected": -1.0,
    "skipped": -0.3,
}


@dataclass
class Touch:
    key: str
    ts: datetime
    weight: float


def touches(events: Iterable[SignalEvent]) -> list[Touch]:
    """One engagement value per title per day. Progress polls count once (0.3), a finish 1,
    thumbs ±, abandons with a reason negative. Capped per day, so a 10-episode day is intense
    but not ten times anything."""
    per_day: dict[tuple[str, str], dict[str, Any]] = {}
    for e in events:
        if e.action in ("cataloged", "added_watchlist", "requested", "detail_viewed", "trailer_played", "searched"):
            continue
        d = per_day.setdefault((e.key, e.timestamp.date().isoformat()), {"w": 0.0, "played": False, "ts": e.timestamp})
        d["ts"] = max(d["ts"], e.timestamp)
        if e.action == "played":
            if not d["played"]:
                d["w"] += 0.3
                d["played"] = True
        elif e.action == "rated":
            v = e.metadata.get("value")
            v = int(v) if isinstance(v, (int, float)) else (1 if e.action_weight > 0 else -1 if e.action_weight < 0 else 0)
            d["w"] += {2: 2.0, 1: 1.0, -1: -1.5}.get(v, 0.0)
        elif e.action == "abandon_reason":
            d["w"] += {"not_for_me": -1.0, "too_slow": -0.5, "too_intense": -0.3}.get(str(e.metadata.get("reason")), 0.0)
        else:
            d["w"] += TOUCH_WEIGHTS.get(e.action, 0.0)
    out = [Touch(k, d["ts"], float(np.clip(d["w"], -2.0, 2.5))) for (k, _), d in per_day.items() if d["w"] != 0.0]
    out.sort(key=lambda t: t.ts)
    return out


def _rate(ts_weights: list[tuple[datetime, float]], now: datetime, half_life: float) -> float:
    """Exponentially-weighted engagement rate, in weight per 30 days."""
    lam = math.log(2) / half_life
    s = sum(w * math.exp(-lam * max((now - t).total_seconds() / 86400.0, 0.0)) for t, w in ts_weights)
    return s * lam * 30.0


def _context(ts: datetime, tz: ZoneInfo) -> tuple[str, str, str]:
    local = ts.astimezone(tz)
    h = local.hour
    daypart = "night" if h < 6 else "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    weekpart = "weekend" if local.weekday() >= 5 else "weekday"
    season = SEASONS[(local.month % 12) // 3]
    return daypart, weekpart, season


def _lift(counts: dict[str, float], overall: dict[str, float], buckets: tuple[str, ...]) -> dict[str, float]:
    tot = sum(overall.get(b, 0.0) for b in buckets)
    n = sum(counts.get(b, 0.0) for b in buckets)
    if tot <= 0 or n <= 0:
        return {}
    out = {}
    for b in buckets:
        p = (overall.get(b, 0.0) + 1e-3) / (tot + 1e-3 * len(buckets))
        q = (counts.get(b, 0.0) + RHYTHM_PRIOR * p) / (n + RHYTHM_PRIOR)
        out[b] = round(float(np.clip(q / p, 0.5, 2.0)), 3)
    return out


def stretch_band(appetite: float) -> float:
    """Width of the band beyond an interest's edge, in normalised-similarity units."""
    return 0.10 + 0.40 * float(np.clip(appetite, 0.0, 1.0))


def stretch_appetite(shown: int, accepted: int) -> float:
    """Beta(1, 3) prior: start cautious (0.25), widen as stretch suggestions get taken."""
    return (accepted + 1.0) / (shown + 4.0)


def analyze(
    profile: TasteProfile,
    events: list[SignalEvent],
    vectors: dict[str, np.ndarray],
    now: datetime | None = None,
    tz: str = "UTC",
    stretch_stats: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    """Pure: history + current interests -> evolution report (stored as profile.evolution)."""
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(tz)
    K = len(profile.interests)
    tch = [t for t in touches(events) if t.key in vectors]
    report: dict[str, Any] = {"computed_at": now.isoformat(), "timezone": tz, "interests": {}, "timeline": [], "milestones": []}
    shown, accepted = stretch_stats
    appetite = stretch_appetite(shown, accepted)
    report["stretch"] = {"appetite": round(appetite, 3), "band": round(stretch_band(appetite), 3), "shown": shown, "accepted": accepted}
    if not tch or K == 0:
        return report

    C = profile.centroid_matrix()
    keys = sorted({t.key for t in tch})
    V = unit(np.stack([np.asarray(vectors[k], dtype=np.float32) for k in keys]))
    sims = normalise_sim(V @ C.T, profile.baseline)
    best = sims.argmax(1)
    best_sim = sims[np.arange(len(keys)), best]
    assign = {k: (int(b) if s >= ASSIGN_THRESHOLD else -1) for k, b, s in zip(keys, best, best_sim)}
    vec_of = {k: V[i] for i, k in enumerate(keys)}

    pos: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    neg: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    ctx_counts: dict[int, dict[str, dict[str, float]]] = defaultdict(lambda: {"daypart": defaultdict(float), "weekpart": defaultdict(float), "season": defaultdict(float)})
    overall_ctx = {"daypart": defaultdict(float), "weekpart": defaultdict(float), "season": defaultdict(float)}
    monthly: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    monthly_vec: dict[str, np.ndarray] = {}
    for t in tch:
        k = assign[t.key]
        if t.weight > 0:
            pos[k].append((t.ts, t.weight))
            dp, wp, se = _context(t.ts, zone)
            for name, b in (("daypart", dp), ("weekpart", wp), ("season", se)):
                ctx_counts[k][name][b] += t.weight
                overall_ctx[name][b] += t.weight
            m = t.ts.astimezone(zone).strftime("%Y-%m")
            monthly[m][k] += t.weight
            monthly_vec[m] = monthly_vec.get(m, 0) + vec_of[t.key] * t.weight
        else:
            neg[k].append((t.ts, -t.weight))

    fb = _feedback(profile, events, vectors, now)
    span_days = (now - tch[0].ts).total_seconds() / 86400.0
    months = sorted(monthly)
    total_by_k = {k: sum(w for _, w in v) for k, v in pos.items()}
    grand = sum(total_by_k.values()) or 1.0

    peak_share: dict[int, tuple[float, str | None]] = {}
    for k in range(K):
        best_share, best_m = 0.0, None
        for m in months:
            tot = sum(monthly[m].values())
            if tot >= 2.0:
                share = monthly[m].get(k, 0.0) / tot
                if share > best_share:
                    best_share, best_m = share, m
        peak_share[k] = (best_share, best_m)

    for k in range(K):
        it = profile.interests[k]
        p = pos.get(k, [])
        n_ = neg.get(k, [])
        info: dict[str, Any] = {"label": it.label, "kind": it.kind}
        if not p:
            info.update(state="pinned" if it.kind == "pinned" else "stable", trend=0.0, satiation=0.0, rediscovery=0.0, context={},
                        steer=round(float(np.clip(fb["steer"].get(k, 0.0), -1, 1)), 3))
            report["interests"][str(k)] = info
            continue
        first, last = p[0][0], p[-1][0]
        short = _rate(p, now, SHORT_HALF_LIFE_DAYS)
        long_ = _rate(p, now, LONG_HALF_LIFE_DAYS)
        trend = math.tanh(math.log((short + 0.05) / (long_ + 0.05)))
        days_since = (now - last).total_seconds() / 86400.0
        age_days = (now - first).total_seconds() / 86400.0
        pk, pk_month = peak_share[k]

        # satiation: intensity above your own steady state, amplified by declining engagement
        bucket = sum(w * 0.5 ** ((now - t).total_seconds() / 86400.0 / SATIATION_HALF_LIFE_DAYS) for t, w in p)
        steady = (long_ / 30.0) * SATIATION_HALF_LIFE_DAYS / math.log(2)
        capacity = max(2.0, BINGE_FACTOR * steady)
        # A new interest has no "normal pace" yet: discovering something and diving in is
        # enthusiasm, not boredom. Intensity only counts once the interest is established.
        established = (now - first).total_seconds() / 86400.0 > EMERGING_DAYS
        intensity = float(np.clip((bucket - capacity) / capacity, 0.0, 1.0)) if established else 0.0
        recent_cut = now - timedelta(days=21)
        r_pos = sum(w for t, w in p if t >= recent_cut)
        r_neg = sum(w for t, w in n_ if t >= recent_cut)
        h_pos = sum(w for t, w in p if t < recent_cut)
        h_neg = sum(w for t, w in n_ if t < recent_cut)
        recent_neg_share = r_neg / (r_pos + r_neg) if r_pos + r_neg > 0 else 0.0
        hist_neg_share = h_neg / (h_pos + h_neg) if h_pos + h_neg > 0 else 0.0
        drop = float(np.clip((recent_neg_share - hist_neg_share) * 3.0, 0.0, 1.0))
        satiation = float(np.clip(intensity * (0.4 + 0.6 * drop) + 0.5 * drop * (1.0 if r_pos >= 3 else 0.0), 0.0, 1.0))

        rediscovery = 0.0
        if days_since > DORMANT_DAYS and pk >= 0.1:
            state = "dormant"
            rediscovery = float(min(1.0, pk * 2.0) * (1.0 - math.exp(-(days_since - DORMANT_DAYS) / 120.0)))
        elif age_days <= EMERGING_DAYS and short > 0.5:
            state = "emerging"
        elif trend > 0.35:
            state = "growing"
        elif trend < -0.35:
            state = "fading"
        else:
            state = "stable"

        # explicit answers (optional, see questions.py) steer on top of what behaviour says
        satiation = float(max(satiation, fb["sat_boost"].get(k, 0.0)) * (1.0 - fb["sat_block"].get(k, 0.0)))
        rediscovery = float(max(rediscovery, 0.8 * fb["red_boost"].get(k, 0.0)) * (1.0 - fb["red_block"].get(k, 0.0)))
        steer = float(np.clip(fb["steer"].get(k, 0.0), -1.0, 1.0))

        ctx = {}
        if len(p) >= MIN_TOUCHES_FOR_RHYTHM:
            ctx["daypart"] = _lift(ctx_counts[k]["daypart"], overall_ctx["daypart"], DAYPARTS)
            ctx["weekpart"] = _lift(ctx_counts[k]["weekpart"], overall_ctx["weekpart"], WEEKPARTS)
            if span_days >= 300 and len(p) >= 12:
                ctx["season"] = _lift(ctx_counts[k]["season"], overall_ctx["season"], SEASONS)

        info.update(
            state=state,
            trend=round(trend, 3),
            satiation=round(satiation, 3),
            rediscovery=round(rediscovery, 3),
            steer=round(steer, 3),
            recent_rate=round(short, 3),
            baseline_rate=round(long_, 3),
            first_seen=first.date().isoformat(),
            last_seen=last.date().isoformat(),
            peak_month=pk_month,
            peak_share=round(pk, 3),
            lifetime_share=round(total_by_k.get(k, 0.0) / grand, 3),
            context=ctx,
        )
        report["interests"][str(k)] = info

    # timeline: monthly shares, breadth, drift
    prev_vec = None
    biggest = (0.0, None)
    for m in months:
        tot = sum(monthly[m].values())
        shares = {str(k): round(v / tot, 3) for k, v in sorted(monthly[m].items(), key=lambda kv: -kv[1]) if v / tot >= 0.02}
        probs = np.array([v / tot for v in monthly[m].values() if v > 0])
        breadth = float(math.exp(-(probs * np.log(probs)).sum())) if len(probs) else 0.0
        mv = unit(np.asarray(monthly_vec[m], dtype=np.float64))
        drift = float(1.0 - normalise_sim(float(mv @ prev_vec), profile.baseline)) if prev_vec is not None else 0.0
        drift = float(np.clip(drift, 0.0, 2.0))
        if prev_vec is not None and tot >= 3 and drift > biggest[0]:
            biggest = (drift, m)
        prev_vec = mv
        report["timeline"].append({"month": m, "shares": shares, "breadth": round(breadth, 2), "drift": round(drift, 3), "activity": round(tot, 1)})

    report["milestones"] = _milestones(profile, report, monthly, months, biggest)
    return report


FEEDBACK_HALF_LIFE = {"more": 60.0, "less": 60.0, "bring_back": 60.0, "moved_on": 180.0, "break": 14.0, "no_break": 14.0}


def _feedback(profile: TasteProfile, events: list[SignalEvent], vectors: dict[str, np.ndarray], now: datetime) -> dict[str, dict[int, float]]:
    """interest_feedback events -> decaying per-interest steers. Interests are re-clustered
    every cycle, so an answer is matched to today's interest by its anchor title's vector (or,
    without one, by member overlap)."""
    out: dict[str, dict[int, float]] = {n: defaultdict(float) for n in ("steer", "sat_boost", "sat_block", "red_boost", "red_block")}
    fbs = [e for e in events if e.action == "interest_feedback"]
    if not fbs or not profile.interests:
        return out
    C = profile.centroid_matrix()
    for e in fbs:
        k = None
        if e.key in vectors:
            sim = normalise_sim(unit(np.asarray(vectors[e.key], dtype=np.float32)) @ C.T, profile.baseline)
            if sim.max() >= ASSIGN_THRESHOLD:
                k = int(sim.argmax())
        if k is None:
            members = set(e.metadata.get("members") or []) | {e.key}
            overlap = [len(members & set(i.members)) for i in profile.interests]
            if max(overlap) > 0:
                k = int(np.argmax(overlap))
        if k is None:
            continue
        resp = str(e.metadata.get("response") or "")
        if resp not in FEEDBACK_HALF_LIFE:
            continue
        d = 0.5 ** (max((now - e.timestamp).total_seconds() / 86400.0, 0.0) / FEEDBACK_HALF_LIFE[resp])
        if resp == "more":
            out["steer"][k] += d
        elif resp == "less":
            out["steer"][k] -= d
        elif resp == "bring_back":
            out["red_boost"][k] = max(out["red_boost"][k], d)
            out["steer"][k] += 0.5 * d
        elif resp == "moved_on":
            out["red_block"][k] = max(out["red_block"][k], d)
            out["steer"][k] -= 0.5 * d
        elif resp == "break":
            out["sat_boost"][k] = max(out["sat_boost"][k], d)
        elif resp == "no_break":
            out["sat_block"][k] = max(out["sat_block"][k], d)
    return out


def _milestones(profile: TasteProfile, report: dict, monthly: dict, months: list[str], biggest: tuple[float, str | None]) -> list[dict]:
    out: list[dict] = []
    for ks, info in report["interests"].items():
        k = int(ks)
        name = _name(profile, k)
        born = None
        for m in months:
            tot = sum(monthly[m].values())
            if tot >= 2.0 and monthly[m].get(k, 0.0) / tot >= 0.15 and monthly[m].get(k, 0.0) >= 2.0:
                born = m
                break
        if born and months and born != months[0]:
            out.append({"month": born, "kind": "new_interest", "interest": k, "text": f"You got into {name}"})
        # a peak is a phase: well above the interest's lifetime share, and not just its first month
        if (info.get("peak_month") and info.get("peak_share", 0) >= 0.3 and info["peak_month"] != born
                and info["peak_share"] >= 1.5 * max(info.get("lifetime_share", 0.0), 0.05)):
            out.append({"month": info["peak_month"], "kind": "peak", "interest": k, "text": f"Peak of {name} ({info['peak_share']:.0%} of your viewing)"})
        if info.get("state") == "dormant":
            out.append({"month": info["last_seen"][:7], "kind": "went_quiet", "interest": k, "text": f"{name} went quiet"})
    if biggest[1] and biggest[0] >= 0.4:
        out.append({"month": biggest[1], "kind": "big_shift", "interest": None, "text": "Your biggest change in taste"})
    out.sort(key=lambda x: x["month"])
    return out


def _name(profile: TasteProfile, k: int) -> str:
    it = profile.interests[k]
    return it.label or ", ".join(t.split(":", 1)[-1] for t in it.top_tags[:2]) or f"interest {k + 1}"


# ---- request time ----------------------------------------------------------------------------

@dataclass
class Adjustment:
    """Per-interest values used at request time, after strength scaling."""

    factor: np.ndarray  # multiplies importance (retrieval quotas, calibration targets)
    trend: np.ndarray
    satiation: np.ndarray
    rediscovery: np.ndarray
    context: np.ndarray  # lift - 1 for the current moment
    band: float
    context_label: str
    steer: np.ndarray | None = None  # from answers to interest questions (optional)


def adjustment(profile: TasteProfile, at: datetime | None = None, strength: float = 1.0) -> Adjustment | None:
    ev = profile.evolution or {}
    K = len(profile.interests)
    if not ev.get("interests") or K == 0 or strength <= 0:
        return None
    at = at or datetime.now(timezone.utc)
    dp, wp, se = _context(at, ZoneInfo(ev.get("timezone", "UTC")))
    trend = np.zeros(K)
    sat = np.zeros(K)
    red = np.zeros(K)
    ctx = np.zeros(K)
    steer = np.zeros(K)
    for k in range(K):
        info = ev["interests"].get(str(k), {})
        steer[k] = info.get("steer", 0.0)
        trend[k] = info.get("trend", 0.0)
        sat[k] = info.get("satiation", 0.0)
        red[k] = info.get("rediscovery", 0.0)
        c = info.get("context") or {}
        lifts = [c.get("daypart", {}).get(dp), c.get("weekpart", {}).get(wp), c.get("season", {}).get(se)]
        lifts = [x for x in lifts if x]
        ctx[k] = float(np.prod(lifts) ** (1.0 / len(lifts))) - 1.0 if lifts else 0.0
    raw = (1.0 + 0.25 * trend) * (1.0 - 0.4 * sat) * np.sqrt(np.clip(1.0 + ctx, 0.5, 2.0)) * (1.0 + 0.6 * red) * (1.0 + 0.3 * steer)
    factor = np.clip(1.0 + strength * (raw - 1.0), 0.5, 1.8)
    band = (ev.get("stretch") or {}).get("band", stretch_band(0.25))
    return Adjustment(factor, trend * strength, sat * strength, red * strength, ctx * strength, band, f"{wp} {dp}", steer * strength)


def apply_importance(profile: TasteProfile, adj: Adjustment | None) -> None:
    """Shifts interest importances in place (in-memory only, never stored)."""
    if adj is None:
        return
    imp = profile.importance_vector().astype(np.float64) * adj.factor
    imp /= max(imp.sum(), 1e-12)
    for it, v in zip(profile.interests, imp):
        it.importance = float(v)


def stretch_score(sims_norm: np.ndarray, radii: np.ndarray, importance: np.ndarray, band: float) -> np.ndarray:
    """sims_norm: n x K normalised similarities. 1.0 at the middle of the band just beyond an
    interest's edge, 0 inside the interest or too far away. Strong interests stretch more."""
    if sims_norm.size == 0:
        return np.zeros(len(sims_norm))
    centre = radii[None, :] - band / 2.0
    tri = np.clip(1.0 - np.abs(sims_norm - centre) / (band / 2.0), 0.0, 1.0)
    tri = np.where(sims_norm < radii[None, :], tri, 0.0)
    imp_rel = np.sqrt(importance / max(float(importance.max()), 1e-9))
    inside_any = (sims_norm >= radii[None, :]).any(1)
    s = (tri * imp_rel[None, :]).max(1)
    return np.where(inside_any, 0.0, s)
