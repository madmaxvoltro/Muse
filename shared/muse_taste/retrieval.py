"""Stage 1 helpers: candidate generation from several independent generators, fused with
Reciprocal Rank Fusion (Cormack, Clarke & Buettcher, SIGIR 2009).

Single-query vector search recalls only what looks like the *average* of your taste. Real
recall comes from several generators that each fail differently:

  interests    one ANN query per interest medoid, slots proportional to importance (PinnerSage)
  short_term   ANN on the recent-intent vector
  item2item    ANN around your most recent strongly-liked titles ("because you watched X")
  related      TMDB recommendations of your top titles: collaborative signal from millions
               of viewers, which a single household can never generate itself
  household    what similar-tasting people in the same household liked
  backlog      watchlist / requested / trailer-watched but not yet seen
  mood         ANN on an embedded mood text, when asked
  quality      best-rated library items (cold start and safety net)

RRF needs no score calibration between generators (cosine, support counts and ratings aren't
comparable). An item ranked well by several generators rises, which the ranker also sees as
the `consensus` feature.
"""

from __future__ import annotations

from collections import defaultdict

RRF_K = 60

GENERATOR_WEIGHT = {
    "interests": 1.0,
    "short_term": 0.7,
    "item2item": 0.9,
    "related": 1.0,
    "household": 0.6,
    "backlog": 0.8,
    "mood": 1.2,
    "quality": 0.3,
    "pinned": 0.9,
}


def rrf(ranked_lists: dict[str, list[str]], k: int = RRF_K) -> tuple[dict[str, float], dict[str, set[str]]]:
    scores: dict[str, float] = defaultdict(float)
    sources: dict[str, set[str]] = defaultdict(set)
    for gen, keys in ranked_lists.items():
        w = GENERATOR_WEIGHT.get(gen.split(":", 1)[0], 0.5)
        for rank, key in enumerate(keys):
            scores[key] += w / (k + rank + 1)
            sources[key].add(gen.split(":", 1)[0])
    return dict(scores), dict(sources)


def interest_quotas(importances: list[float], total: int, min_each: int = 12, max_interests: int = 8) -> list[int]:
    """Slots per interest, proportional to importance, a floor so small interests still
    surface, top `max_interests` only."""
    if not importances:
        return []
    idx = sorted(range(len(importances)), key=lambda i: -importances[i])[:max_interests]
    s = sum(importances[i] for i in idx) or 1.0
    quotas = [0] * len(importances)
    for i in idx:
        quotas[i] = max(min_each, round(total * importances[i] / s))
    return quotas


def related_support(top_items: list[tuple[str, float, str]], related_of: dict[str, list[str]]) -> dict[str, tuple[float, list[str]]]:
    """Borrowed collaborative filtering: for each of your strongest titles (key, weight, title),
    TMDB's recommendation list for it votes for its members, discounted by rank.
    Returns candidate -> (support, [titles that vouched for it])."""
    out: dict[str, list] = {}
    for key, w, title in top_items:
        for rank, rel in enumerate(related_of.get(key, [])[:20]):
            if rel == key:
                continue
            e = out.setdefault(rel, [0.0, []])
            e[0] += max(w, 0.0) / (1.0 + rank / 5.0)
            if title and len(e[1]) < 3:
                e[1].append(title)
    return {k: (v[0], v[1]) for k, v in out.items()}


def household_support(me: dict[str, float], others: dict[str, dict[str, float]], min_overlap: int = 3) -> dict[str, float]:
    """User-user CF inside the household: weight each other person by agreement on shared
    titles (cosine over preferences), then their liked titles vote. Needs >= min_overlap shared
    judgements, so two people who merely live together don't merge."""
    out: dict[str, float] = defaultdict(float)
    for _, theirs in others.items():
        shared = set(me) & set(theirs)
        if len(shared) < min_overlap:
            continue
        num = sum(me[k] * theirs[k] for k in shared)
        den = (sum(me[k] ** 2 for k in shared) ** 0.5) * (sum(theirs[k] ** 2 for k in shared) ** 0.5)
        sim = num / den if den else 0.0
        if sim <= 0.1:
            continue
        for k, p in theirs.items():
            if p > 0 and k not in me:
                out[k] += sim * p
    return dict(out)
