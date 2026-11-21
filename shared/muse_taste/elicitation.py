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


def this_or_that(pool: list[Scored], baseline: float, asked: set[str] | None = None) -> tuple[Scored, Scored] | None:
    """Picks (a, b) maximising sigma_a * sigma_b * plausibility * dissimilarity."""
    asked = asked or set()
    cands = [s for s in pool if s.key not in asked and s.features.get("negative", 0) < 0.5]
    if len(cands) < 2:
        return None
    scores = np.array([s.score for s in cands])
    mu, sd = float(scores.mean()), float(scores.std() or 1.0)
    plaus = {s.key: 1.0 / (1.0 + math.exp(-(s.score - mu) / sd)) for s in cands}
    a = max(cands, key=lambda s: s.sigma * plaus[s.key])
    best, best_v = None, -1.0
    for s in cands:
        if s is a:
            continue
        sim = float(normalise_sim(float(a.vector @ s.vector), baseline))
        diff_interest = 1.0 if (a.interest_id is not None and s.interest_id != a.interest_id) else 0.6
        v = s.sigma * plaus[s.key] * (1.0 - max(sim, 0.0)) * diff_interest
        if v > best_v:
            best, best_v = s, v
    if best is None:
        return None
    return a, best


def new_question_id() -> str:
    return uuid.uuid4().hex
