"""Stage 3: turning a scored pool into a good *list*.

A list of the 20 highest scores is usually a bad list: ten near-identical titles from your
biggest interest, two sequels of the same franchise, and nothing that teaches Muse anything.
The selection is one greedy pass that, at every slot, picks the candidate maximising

    (1 - lam_cal) * score  +  lam_cal * calibration_gain  -  lam_red * redundancy

* **Calibration** (Steck, "Calibrated Recommendations", RecSys 2018): the list's mix over
  your interests should match your interests' proportions (70% crime / 30% animation ->
  roughly 70/30, not 100/0). We calibrate over the *interest clusters* rather than genres;
  Kaya & Bridge (RecSys 2019) found intent-aware selection over subprofiles gives the best
  relevance/diversity trade-off. The gain is the marginal drop in KL(p || q) from adding the
  item, with Steck's smoothing so the KL stays finite.
* **Redundancy** (MMR, Carbonell & Goldstein 1998): penalise similarity to what's already
  picked.
* **Collections**: one entry per franchise, and the earliest part you haven't seen. Recommend
  part 1 before part 4.

**Exploration** uses Thompson sampling (Chapelle & Li, "An Empirical Evaluation of Thompson
Sampling", NeurIPS 2011): each candidate's score is drawn from N(score, sigma^2), and explore
slots go to the best *draws* among genuinely uncertain candidates. Promising-but-unknown
items get shown, their feedback shrinks sigma, and the next draw is better informed: the
multi-armed-bandit loop docs/architecture.md describes. Draws are seeded per user and day,
so a row is stable within a day and rotates between days.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date

import numpy as np

from .profile import TasteProfile, normalise_sim
from .ranking import Scored

CAL_LAMBDA = 0.35
RED_LAMBDA = 0.30
CAL_ALPHA = 0.02  # Steck's smoothing of q toward p
OTHER_SHARE = 0.05  # target share for items outside every interest (exploit part of the list)


def _kl(p: np.ndarray, q: np.ndarray) -> float:
    mask = p > 0
    return float((p[mask] * np.log(p[mask] / np.maximum(q[mask], 1e-12))).sum())


def dedupe_collections(pool: list[Scored]) -> list[Scored]:
    """Within a collection keep the earliest-released candidate (seen parts are already gone,
    so that's the next part you haven't watched) and give it the group's best score."""
    groups: dict[str, list[Scored]] = {}
    rest: list[Scored] = []
    for s in pool:
        coll = s.payload.get("collection_id")
        if coll:
            groups.setdefault(str(coll), []).append(s)
        else:
            rest.append(s)
    for members in groups.values():
        members.sort(key=lambda s: (s.payload.get("year") or 9999))
        first = members[0]
        best = max(members, key=lambda s: s.score)
        if best is not first:
            first.score = max(first.score, best.score - 0.05)
            first.contributions = dict(best.contributions)
            first.interest_id = best.interest_id
            first.membership = best.membership
        rest.append(first)
    return rest


def thompson(pool: list[Scored], seed: int) -> None:
    rng = np.random.default_rng(seed)
    eps = rng.standard_normal(len(pool))
    for s, e in zip(pool, eps):
        s.sampled = s.score + s.sigma * float(e)


def daily_seed(user_id: str, surface: str, day: date | None = None) -> int:
    day = day or date.today()
    return int(hashlib.sha256(f"{user_id}:{surface}:{day.isoformat()}".encode()).hexdigest()[:8], 16)


def select(
    pool: list[Scored],
    profile: TasteProfile,
    limit: int,
    explore_ratio: float = 0.125,
    seed: int = 0,
    lam_cal: float = CAL_LAMBDA,
    lam_red: float = RED_LAMBDA,
) -> list[Scored]:
    if not pool or limit <= 0:
        return []
    pool = dedupe_collections(pool)
    thompson(pool, seed)

    n_explore = 0
    if explore_ratio > 0 and len(pool) > limit:
        n_explore = max(1, round(limit * explore_ratio))
    n_exploit = limit - n_explore

    K = len(profile.interests)
    # Target distribution over interests plus an explicit "other" bucket: items that belong
    # to no interest go there, and its small target share means filling it is penalised
    # exactly like over-filling any one interest.
    if K:
        imp = profile.importance_vector().astype(np.float64)
        p = np.append((1 - OTHER_SHARE) * imp / max(imp.sum(), 1e-12), OTHER_SHARE)
    else:
        p = np.zeros(0)
    b = profile.baseline

    selected: list[Scored] = []
    counts = np.zeros(K + 1) if K else np.zeros(0)
    sel_vecs: list[np.ndarray] = []

    score_arr = np.array([s.score for s in pool])
    spread = max(float(np.percentile(score_arr, 90) - np.percentile(score_arr, 10)), 0.2)

    def membership(s: Scored) -> np.ndarray | None:
        if K == 0 or s.membership.size != K:
            return None
        m = np.clip(s.membership, 0.0, 1.0)
        return np.append(m, max(1.0 - float(m.sum()), 0.0))

    def smoothed(c: np.ndarray) -> np.ndarray:
        q = c / max(c.sum(), 1e-12)
        return (1 - CAL_ALPHA) * q + CAL_ALPHA * p

    def calib_gain(s: Scored) -> float:
        m = membership(s)
        if m is None:
            return 0.0
        before = _kl(p, smoothed(counts)) if counts.sum() > 0 else 0.0
        return before - _kl(p, smoothed(counts + m))

    def redundancy(s: Scored) -> float:
        if not sel_vecs:
            return 0.0
        sims = normalise_sim(np.stack(sel_vecs) @ s.vector, b)
        return float(np.clip(sims.max(), 0.0, 1.0))

    def pick(candidates: list[Scored], key) -> Scored | None:
        best, best_v = None, -math.inf
        for s in candidates:
            v = key(s)
            if v > best_v:
                best, best_v = s, v
        return best

    remaining = list(pool)
    for _ in range(min(n_exploit, len(remaining))):
        s = pick(
            remaining,
            lambda s: (1 - lam_cal) * s.score / spread + lam_cal * calib_gain(s) * 4.0 - lam_red * redundancy(s),
        )
        if s is None:
            break
        remaining.remove(s)
        selected.append(s)
        sel_vecs.append(s.vector)
        m = membership(s)
        if m is not None:
            counts += m

    explore: list[Scored] = []
    if n_explore and remaining:
        sigmas = np.array([s.sigma for s in remaining])
        cutoff = float(np.median(sigmas))
        uncertain = [s for s in remaining if s.sigma >= cutoff and s.features.get("negative", 0) < 0.5]
        floor = np.percentile(score_arr, 25)  # promising, not random
        uncertain = [s for s in uncertain if s.score >= floor] or uncertain
        for _ in range(min(n_explore, len(uncertain))):
            s = pick(uncertain, lambda s: s.sampled - lam_red * redundancy(s))
            if s is None:
                break
            uncertain.remove(s)
            s.explore = True
            explore.append(s)
            sel_vecs.append(s.vector)

    # Interleave explore picks at even spacing, not dumped at the end where nobody looks.
    if explore:
        out = list(selected)
        step = max(len(out) // (len(explore) + 1), 1)
        for j, e in enumerate(explore):
            pos = min(step * (j + 1) + j, len(out))
            out.insert(pos, e)
        selected = out
    return selected[:limit]


def intra_list_diversity(items: list[Scored], baseline: float) -> float:
    if len(items) < 2:
        return 0.0
    V = np.stack([s.vector for s in items])
    S = normalise_sim(V @ V.T, baseline)
    n = len(items)
    return float(1.0 - (S.sum() - np.trace(S)) / (n * (n - 1)))
