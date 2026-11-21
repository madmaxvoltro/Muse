"""Stage 2: ranking. Every candidate gets a feature vector, a score and an uncertainty.

The score is a linear model over named features, so its explanation is exact: each
feature's contribution w_i * x_i *is* the reason. The weights start hand-set (below) and can
be re-fitted from logged impressions once there is data (learn.py), with a prior that pulls
them back toward the hand-set values when evidence is thin.

Uncertainty is per *candidate*, not per genre. It comes from how much judged evidence sits
near the candidate in embedding space (a kernel-weighted count of anchors) and whether that
evidence agrees. A candidate surrounded by ten loved films is a confident match. One in a
region you have never touched is a guess, and those guesses are what the explore slots are
for (explore.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .evolution import Adjustment, stretch_score
from .profile import TasteProfile, normalise_sim, unit

DEFAULT_WEIGHTS: dict[str, float] = {
    "interest": 1.00,  # closeness to one of your interests, importance-aware
    "short_term": 0.35,  # what you've been into lately
    "item_sim": 0.45,  # very close to a specific title you rated/loved
    "tags": 0.45,  # director / keywords / cast / studio you have a track record with
    "negative": -1.00,  # close to things you disliked
    "related": 0.50,  # TMDB "fans of X also liked" from your strongest titles
    "household": 0.25,  # liked by others in the household with similar taste
    "intent": 0.45,  # on your watchlist / requested / trailer watched
    "quality": 0.25,  # Bayesian-average rating
    "freshness": 0.10,  # new to the library
    "fatigue": -0.50,  # shown repeatedly, never picked
    "consensus": 0.15,  # surfaced by several independent generators
    "mood": 0.80,  # matches the mood text you asked for (only when given)
    # taste evolution (muse_taste.evolution), all zero when there's no history to go on
    "trend": 0.20,  # belongs to an interest you're watching more of lately
    "satiation": -0.40,  # belongs to an interest you've had a lot of and are tiring of
    "rediscovery": 0.30,  # belongs to a dormant interest you used to love
    "context": 0.30,  # belongs to an interest you tend to watch at this time / day / season
    "stretch": 0.15,  # just beyond the edge of one of your interests (broadening)
}

FEATURE_TEXT = {
    "interest": "matches your taste for {interest}",
    "short_term": "in line with what you've watched lately",
    "item_sim": "similar to {anchor}",
    "tags": "{tags}",
    "related": "fans of {related} also liked this",
    "household": "popular in your household",
    "intent": "on your list",
    "quality": "highly rated ({rating})",
    "freshness": "new in your library",
    "consensus": "",
    "mood": "fits the mood you asked for",
}

SIGMA0 = 0.5  # prior std of the score, in score units
QUALITY_PRIOR_MEAN = 6.6
QUALITY_PRIOR_VOTES = 250.0


@dataclass
class CandidateInput:
    key: str
    vector: np.ndarray
    tags: list[str] = field(default_factory=list)
    vote_average: float | None = None
    vote_count: float | None = None
    added_days_ago: float | None = None
    related_support: float = 0.0
    related_via: list[str] = field(default_factory=list)
    household_support: float = 0.0
    impressions_unengaged: int = 0
    generators: set[str] = field(default_factory=set)
    rrf: float = 0.0
    payload: dict = field(default_factory=dict)


@dataclass
class Scored:
    key: str
    score: float
    sigma: float
    confidence: float
    features: dict[str, float]
    contributions: dict[str, float]
    interest_id: int | None
    membership: np.ndarray
    vector: np.ndarray
    nearest_anchor: str | None
    top_tags: list[str]
    payload: dict
    explore: bool = False
    sampled: float = 0.0


def bayesian_rating(avg: float | None, votes: float | None, prior_mean: float = QUALITY_PRIOR_MEAN, m: float = QUALITY_PRIOR_VOTES) -> float | None:
    """IMDb-style weighted rating: few votes -> pulled to the prior mean."""
    if avg is None or votes is None or votes <= 0:
        return None
    return (votes / (votes + m)) * avg + (m / (votes + m)) * prior_mean


def score_candidates(
    profile: TasteProfile,
    cands: list[CandidateInput],
    anchor_vectors: dict[str, np.ndarray] | None = None,
    weights: dict[str, float] | None = None,
    mood_vector: np.ndarray | None = None,
    membership_temperature: float = 0.07,
    evolution: Adjustment | None = None,
) -> list[Scored]:
    if not cands:
        return []
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    b = profile.baseline
    X = unit(np.stack([np.asarray(c.vector, dtype=np.float32) for c in cands]))
    n = len(cands)

    # --- interests ----------------------------------------------------------------------
    C = profile.centroid_matrix()
    if C.size:
        sims = normalise_sim(X @ C.T, b)  # n x K
        imp = profile.importance_vector()
        imp_rel = imp / max(float(imp.max()), 1e-9)
        # A strong match with a small interest still counts; importance only nudges.
        adj = sims * (0.8 + 0.2 * imp_rel[None, :])
        best = adj.argmax(1)
        interest_feat = adj[np.arange(n), best]
        logits = sims / membership_temperature
        logits -= logits.max(1, keepdims=True)
        member = np.exp(logits)
        member /= member.sum(1, keepdims=True)
        # Membership carries relevance as mass: an item close to no interest adds (almost)
        # nothing to the list's interest distribution. Without this, an item equally far from
        # everything gets an even split that "perfectly" matches a balanced profile, and
        # calibration would reward irrelevant picks.
        member *= np.clip(sims.max(1, keepdims=True), 0.0, 1.0)
    else:
        best = np.full(n, -1)
        interest_feat = np.zeros(n)
        member = np.zeros((n, 0))
        sims = np.zeros((n, 0))

    # taste evolution: per-interest values, spread over each candidate's interest membership
    evo = {f: np.zeros(n) for f in ("trend", "satiation", "rediscovery", "context", "stretch")}
    stretch_k = np.full(n, -1)
    if evolution is not None and member.size:
        mn = member / np.maximum(member.sum(1, keepdims=True), 1e-9)
        evo["trend"] = mn @ evolution.trend
        evo["satiation"] = mn @ evolution.satiation
        evo["rediscovery"] = mn @ evolution.rediscovery
        evo["context"] = mn @ evolution.context
        radii = np.array([i.radius for i in profile.interests])
        evo["stretch"] = stretch_score(sims, radii, profile.importance_vector(), evolution.band)
        stretch_k = (sims - radii[None, :]).argmax(1)

    short = normalise_sim(X @ np.asarray(profile.short_term, dtype=np.float32), b) if profile.short_term else np.zeros(n)

    # --- negatives: strongest weighted similarity to a disliked item, above a margin ---------
    N, nw = profile.negative_matrix()
    if N.size:
        ns = normalise_sim(X @ N.T, b)
        neg_feat = (np.clip((ns - 0.35) / 0.65, 0, 1) * (nw[None, :] / 1.5)).max(1)
    else:
        neg_feat = np.zeros(n)

    # --- anchors: item-to-item similarity + local evidence (uncertainty) ----------------------
    anchor_vectors = anchor_vectors or {}
    anchors = [a for a in profile.anchors if a.key in anchor_vectors]
    if anchors:
        A = unit(np.stack([np.asarray(anchor_vectors[a.key], dtype=np.float32) for a in anchors]))
        pref = np.array([a.preference for a in anchors], dtype=np.float32)
        conf = np.array([a.confidence for a in anchors], dtype=np.float32)
        aw = np.array([a.weight for a in anchors], dtype=np.float32)
        asim = normalise_sim(X @ A.T, b)  # n x A
        # Soft kernel: cubed normalised similarity. Near-duplicates count fully, same-subgenre
        # neighbours partly, and the long tail of unrelated anchors adds almost nothing.
        kern = np.clip(asim, 0, 1) ** 3
        n_eff = (kern * np.log1p(conf)[None, :]).sum(1)
        signed = (kern * aw[None, :]).sum(1)
        absmass = (kern * np.abs(aw)[None, :]).sum(1)
        disagreement = np.where(absmass > 1e-6, 1.0 - np.abs(signed) / np.maximum(absmass, 1e-6), 0.0)
        pos_mask = pref > 0
        if pos_mask.any():
            pos_sim = asim[:, pos_mask] * np.clip(pref[pos_mask] / 2.0, 0.3, 1.0)[None, :]
            nearest_idx = pos_sim.argmax(1)
            item_sim = np.clip((pos_sim[np.arange(n), nearest_idx] - 0.45) / 0.55, 0, 1)
            pos_keys = [a.key for a, m in zip(anchors, pos_mask) if m]
            nearest = [pos_keys[i] for i in nearest_idx]
        else:
            item_sim = np.zeros(n)
            nearest = [None] * n
    else:
        n_eff = np.zeros(n)
        disagreement = np.zeros(n)
        item_sim = np.zeros(n)
        nearest = [None] * n

    mood = normalise_sim(X @ unit(np.asarray(mood_vector, dtype=np.float32)), b) if mood_vector is not None else None

    out: list[Scored] = []
    for i, c in enumerate(cands):
        tag_raw = 0.0
        hits: list[tuple[str, float]] = []
        for t in c.tags:
            tw = profile.tag_weights.get(t)
            if tw:
                tag_raw += tw
                hits.append((t, tw))
        tag_feat = math.tanh(tag_raw / math.sqrt(max(len(c.tags), 1)) / profile.tag_scale * 2.0) if c.tags else 0.0

        br = bayesian_rating(c.vote_average, c.vote_count)
        quality = float(np.clip((br - 6.0) / 2.0, -1.0, 1.0)) if br is not None else 0.0
        fresh = math.exp(-c.added_days_ago / 21.0) if c.added_days_ago is not None else 0.0
        feats = {
            "interest": float(np.clip(interest_feat[i], -1, 1)),
            "short_term": float(np.clip(short[i], -1, 1)),
            "item_sim": float(item_sim[i]),
            "tags": float(tag_feat),
            "negative": float(neg_feat[i]),
            "related": math.tanh(c.related_support),
            "household": math.tanh(c.household_support),
            "intent": float(profile.backlog.get(c.key, 0.0)) / 0.55,
            "quality": quality,
            "freshness": fresh,
            "fatigue": 1.0 - math.exp(-c.impressions_unengaged / 3.0),
            "consensus": min(max(len(c.generators) - 1, 0) / 3.0, 1.0),
        }
        if mood is not None:
            feats["mood"] = float(np.clip(mood[i], -1, 1))
        for f, arr in evo.items():
            feats[f] = float(arr[i])
        if feats["stretch"] and feats["negative"] > 0.3:
            feats["stretch"] = 0.0  # never "broaden" toward something you dislike
        contrib = {k: w.get(k, 0.0) * v for k, v in feats.items()}
        score = float(sum(contrib.values()))

        sigma = SIGMA0 / math.sqrt(1.0 + float(n_eff[i])) * (1.0 + float(disagreement[i]))
        confidence = float(np.clip(1.0 - sigma / (SIGMA0 * 2.0), 0.0, 1.0))
        hits.sort(key=lambda t: -t[1])
        out.append(
            Scored(
                key=c.key,
                score=score,
                sigma=sigma,
                confidence=confidence,
                features=feats,
                contributions=contrib,
                interest_id=int(best[i]) if best[i] >= 0 else None,
                membership=member[i] if member.size else np.zeros(0),
                vector=X[i],
                nearest_anchor=nearest[i],
                top_tags=[t for t, v in hits[:3] if v > 0],
                payload={**c.payload, "related_via": c.related_via, "generators": sorted(c.generators),
                         "stretch_interest": int(stretch_k[i]) if feats["stretch"] > 0 else None,
                         "context_label": evolution.context_label if evolution is not None else None},
            )
        )
    return out


def explain(s: Scored, profile: TasteProfile, titles: dict[str, str], max_reasons: int = 3) -> list[str]:
    """Human sentences for the strongest positive contributions, e.g.
    ["Because you like Slow-burn Nordic crime", "Similar to The Killing", "Directed by Denis Villeneuve"]."""
    reasons: list[tuple[float, str]] = []
    for k, v in sorted(s.contributions.items(), key=lambda kv: -kv[1]):
        if v <= 0.04:
            continue
        if k == "interest" and s.interest_id is not None and s.interest_id < len(profile.interests):
            it = profile.interests[s.interest_id]
            label = it.label or ", ".join(_pretty_tag(t) for t in it.top_tags[:2]) or titles.get(it.medoid_key, "")
            if label:
                reasons.append((v, f"Because you like {label}"))
        elif k == "item_sim" and s.nearest_anchor:
            t = titles.get(s.nearest_anchor)
            if t:
                reasons.append((v, f"Similar to {t}"))
        elif k == "tags" and s.top_tags:
            reasons.append((v, _tag_reason(s.top_tags[0])))
        elif k == "related" and s.payload.get("related_via"):
            reasons.append((v, f"Fans of {s.payload['related_via'][0]} also liked this"))
        elif k == "intent":
            reasons.append((v, "On your list"))
        elif k == "quality":
            r = s.payload.get("vote_average")
            reasons.append((v, f"Highly rated ({r:.1f})" if r else "Highly rated"))
        elif k == "freshness":
            reasons.append((v, "New in your library"))
        elif k == "household":
            reasons.append((v, "Liked by others in your household"))
        elif k == "short_term":
            reasons.append((v, "In line with what you've been watching lately"))
        elif k == "mood":
            reasons.append((v, "Fits your mood"))
        elif k == "trend" and s.interest_id is not None:
            reasons.append((v, f"You've been watching more {_interest_name(profile, s.interest_id, titles)} lately"))
        elif k == "rediscovery" and s.interest_id is not None:
            reasons.append((v, f"It's been a while since your {_interest_name(profile, s.interest_id, titles)} phase"))
        elif k == "context" and s.interest_id is not None and s.payload.get("context_label"):
            reasons.append((v, f"You often pick {_interest_name(profile, s.interest_id, titles)} on {s.payload['context_label']}s"))
        elif k == "stretch" and s.payload.get("stretch_interest") is not None:
            reasons.append((v, f"One step beyond {_interest_name(profile, s.payload['stretch_interest'], titles)}"))
    if s.explore:
        reasons.append((0.0, "Something different, to learn your taste"))
    seen = set()
    out = []
    for _, r in reasons:
        if r not in seen:
            out.append(r)
            seen.add(r)
    return out[:max_reasons]


_TAG_PREFIX_TEXT = {
    "d": "Directed by {}",
    "w": "Written by {}",
    "p": "With {}",
    "s": "From {}",
    "n": "From {}",
    "a": "By {}",
    "au": "By {}",
    "ch": "From {}",
    "k": "About {}",
    "th": "About {}",
    "t": "{}",
    "g": "{}",
    "c": "Continues a series you like",
}


def _interest_name(profile: TasteProfile, k: int, titles: dict[str, str]) -> str:
    if k is None or k >= len(profile.interests):
        return "this"
    it = profile.interests[k]
    return it.label or ", ".join(_pretty_tag(t) for t in it.top_tags[:2]) or titles.get(it.medoid_key, "this")


def _pretty_tag(tag: str) -> str:
    ns, _, v = tag.partition(":")
    return v.title() if ns in ("d", "w", "p", "s", "n", "a", "au", "ch") else v


def _tag_reason(tag: str) -> str:
    ns, _, v = tag.partition(":")
    tmpl = _TAG_PREFIX_TEXT.get(ns, "{}")
    return tmpl.format(_pretty_tag(tag)) if "{}" in tmpl else tmpl


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
