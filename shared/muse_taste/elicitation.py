"""Muse asks questions, but only ones whose answers change what it recommends.

Three question types:

1. **Onboarding picks**: a new profile sees ~24 titles spread across the catalog and taps the
   ones it loves (and optionally "not for me"). The titles are chosen by popularity-weighted
   farthest-point sampling: each pick is far from everything already shown, so few answers
   cover the whole taste space, and well-known, so people can actually judge them.
   (Cold-start elicitation by representative items: Rashid et al., "Getting to Know You:
   Learning New User Preferences in Recommender Systems", IUI 2002; Golbandi, Koren & Lempel,
   "Adaptive Bootstrapping of Recommender Systems Using Decision Trees", WSDM 2011.)

2. **This or that?**: pairwise questions chosen by uncertainty sampling, the active-learning
   principle that labels are worth most where the model is least sure. Both titles are
   plausible (decent predicted score), uncertain (high sigma, little evidence nearby) and
   different from each other (different interests or regions), so either answer moves the
   profile. Pairwise questions are easier and more consistent for people than absolute
   ratings (Jones, Brun & Boyer, "Comparisons Instead of Ratings", 2011).

3. **Why did you stop?**: for titles abandoned early with no reason known. "Not in the mood"
   and "not for me" look identical in viewing data but mean opposite things. Asking keeps a
   mood-driven stop from becoming a false dislike.
"""

from __future__ import annotations

import math
import uuid

import numpy as np

from .profile import normalise_sim, unit
from .ranking import Scored

ABANDON_REASONS = [
    ("not_for_me", "Not for me"),
    ("not_in_mood", "Not in the mood right now"),
    ("too_slow", "Too slow"),
    ("too_intense", "Too intense / scary"),
    ("will_finish_later", "I'll finish it later"),
    ("watched_elsewhere", "Already saw it elsewhere"),
    ("bad_quality", "Playback / quality problem"),
]


def onboarding_spread(
    keys: list[str],
    vectors: np.ndarray,
    popularity: np.ndarray,
    n: int = 24,
    baseline: float = 0.2,
    exclude: set[str] | None = None,
    popularity_power: float = 0.6,
) -> list[str]:
    exclude = exclude or set()
    idx = [i for i, k in enumerate(keys) if k not in exclude]
    if not idx:
        return []
    V = unit(vectors[idx].astype(np.float64))
    pop = np.asarray(popularity, dtype=np.float64)[idx]
    pop = np.log1p(np.maximum(pop, 0))
    pop = pop / max(pop.max(), 1e-9)
    pw = (0.15 + 0.85 * pop) ** popularity_power

    chosen = [int(np.argmax(pw))]
    min_dist = 1.0 - normalise_sim(V @ V[chosen[0]], baseline)
    while len(chosen) < min(n, len(idx)):
        score = np.clip(min_dist, 0, None) * pw
        score[chosen] = -1
        j = int(np.argmax(score))
        if score[j] <= 0:
            break
        chosen.append(j)
        min_dist = np.minimum(min_dist, 1.0 - normalise_sim(V @ V[j], baseline))
    return [keys[idx[j]] for j in chosen]


def influence(pool: list[Scored], baseline: float) -> np.ndarray:
    """How many plausible candidates an answer about each item would inform: the kernel-weighted
    sum of plausibility over its neighbours. Active-learning criteria for recommenders (Elahi,
    Ricci & Rubens, "A survey of active learning in collaborative filtering recommender
    systems", 2016) favour items whose feedback propagates to many predictions over isolated
    outliers. Normalised to [0, 1]."""
    if len(pool) < 2:
        return np.ones(len(pool))
    V = np.stack([s.vector for s in pool])
    scores = np.array([s.score for s in pool])
    plaus = 1.0 / (1.0 + np.exp(-(scores - scores.mean()) / (scores.std() or 1.0)))
    K = np.clip(normalise_sim(V @ V.T, baseline), 0.0, 1.0) ** 2
    np.fill_diagonal(K, 0.0)
    inf = K @ plaus
    return inf / max(float(inf.max()), 1e-9)


def this_or_that(pool: list[Scored], baseline: float, asked: set[str] | None = None) -> tuple[Scored, Scored] | None:
    """Picks (a, b) maximising expected information: each side uncertain (sigma), plausible,
    influential (its answer informs many other candidates), and the two far apart, preferably
    from different interests, so either answer moves the profile. Pairwise questions with
    explore-exploit style selection learned preferences fastest in Christakopoulou, Radlinski
    & Hofmann, "Towards Conversational Recommender Systems" (KDD 2016)."""
    asked = asked or set()
    cands = [s for s in pool if s.key not in asked and s.features.get("negative", 0) < 0.5]
    if len(cands) < 2:
        return None
    scores = np.array([s.score for s in cands])
    mu, sd = float(scores.mean()), float(scores.std() or 1.0)
    plaus = {s.key: 1.0 / (1.0 + math.exp(-(s.score - mu) / sd)) for s in cands}
    inf = dict(zip((s.key for s in cands), influence(cands, baseline)))
    value = {s.key: s.sigma * plaus[s.key] * (0.3 + 0.7 * inf[s.key]) for s in cands}
    a = max(cands, key=lambda s: value[s.key])
    best, best_v = None, -1.0
    for s in cands:
        if s is a:
            continue
        sim = float(normalise_sim(float(a.vector @ s.vector), baseline))
        diff_interest = 1.0 if (a.interest_id is not None and s.interest_id != a.interest_id) else 0.6
        v = value[s.key] * (1.0 - max(sim, 0.0)) * diff_interest
        if v > best_v:
            best, best_v = s, v
    if best is None:
        return None
    return a, best


# Critiquing questions (system-suggested critiques: Chen & Pu, "Critiquing-based recommenders:
# survey and emerging trends", UMUAI 2012): one tap on an attribute shifts many candidates at
# once, where a rating moves one.
TAG_QUESTION_NS = {"d": 1.0, "k": 0.9, "th": 0.9, "t": 0.85, "p": 0.7, "w": 0.6, "s": 0.5, "g": 0.6, "a": 1.0, "au": 1.0, "ch": 0.9}
TAG_PROMPTS = {
    "d": "How do you feel about films and shows by {x}?",
    "w": "How do you feel about stories written by {x}?",
    "p": "How do you feel about titles with {x}?",
    "k": "How do you feel about stories involving {x}?",
    "th": "How do you feel about stories about {x}?",
    "t": "How do you feel about things that are {x}?",
    "g": "How do you feel about {x}?",
    "s": "How do you feel about titles from {x}?",
    "a": "How do you feel about music by {x}?",
    "au": "How do you feel about books by {x}?",
    "ch": "How do you feel about videos from {x}?",
}


def pretty_tag(tag: str) -> str:
    ns, _, v = tag.partition(":")
    return v.title() if ns in ("d", "w", "p", "s", "a", "au", "ch") else v


def tag_question(pool: list[Scored], tag_weights: dict[str, float], tag_scale: float, asked: set[str] | None = None,
                 top_n: int = 120, min_items: int = 4) -> tuple[str, float, list[str]] | None:
    """The tag whose answer would change the most: frequent among your strongest candidates
    (coverage, weighted by plausibility), and one Muse knows little about (small |weight|).
    -> (tag, value, example keys) or None."""
    asked = asked or set()
    top = sorted(pool, key=lambda s: -s.score)[:top_n]
    if not top:
        return None
    scores = np.array([s.score for s in top])
    mu, sd = float(scores.mean()), float(scores.std() or 1.0)
    cover: dict[str, float] = {}
    count: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for s in top:
        p = 1.0 / (1.0 + math.exp(-(s.score - mu) / sd))
        for t in s.payload.get("tags") or []:
            ns = t.split(":", 1)[0]
            if ns not in TAG_QUESTION_NS or t in asked:
                continue
            cover[t] = cover.get(t, 0.0) + p
            count[t] = count.get(t, 0) + 1
            if len(examples.setdefault(t, [])) < 3:
                examples[t].append(s.key)
    best, best_v = None, 0.0
    for t, c in cover.items():
        if count[t] < min_items or count[t] > 0.6 * len(top):  # too rare to matter, or too common to discriminate
            continue
        certainty = min(abs(tag_weights.get(t, 0.0)) / max(0.5 * tag_scale, 1e-6), 1.0)
        v = (c / len(top)) * (1.0 - certainty) * TAG_QUESTION_NS[t.split(":", 1)[0]]
        if v > best_v:
            best, best_v = t, v
    return (best, best_v, examples[best]) if best else None


def new_question_id() -> str:
    return uuid.uuid4().hex
