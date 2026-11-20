"""Taste geometry: facets, affinities, group blending and diversity. Pure numpy, no I/O.

Why facets: a single taste vector is the weighted mean of everything someone liked. For a
person who loves both slow cerebral science fiction and warm romantic comedies, that mean
sits somewhere between the two, close to neither, and recommendations drift into a bland
middle. Clustering the liked items into a few facets and generating candidates per facet
keeps each side of a taste sharp; the facet weights keep the bigger side bigger.
"""

from dataclasses import dataclass, field

import numpy as np

MAX_FACETS = 4
MIN_ITEMS_PER_FACET = 4


def unit(v) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else a


def cos(a, b) -> float:
    return float(np.dot(unit(a), unit(b)))


@dataclass
class Facet:
    centroid: np.ndarray
    weight: float  # share of the positive weight, 0-1
    members: list[int] = field(default_factory=list)  # indexes into the anchors


def facets(vectors: list, weights: list[float], k_max: int = MAX_FACETS, iters: int = 12, seed: int = 7) -> list[Facet]:
    """Weighted spherical k-means over liked items. k grows with the evidence: one facet per
    MIN_ITEMS_PER_FACET liked items, at most k_max. Deterministic (k-means++ with a fixed
    seed), so the same history gives the same rows."""
    if not vectors:
        return []
    X = np.stack([unit(v) for v in vectors])
    w = np.clip(np.asarray(weights, dtype=np.float32), 1e-6, None)
    k = int(max(1, min(k_max, len(vectors) // MIN_ITEMS_PER_FACET)))
    if k == 1:
        return [Facet(unit((X * w[:, None]).sum(0)), 1.0, list(range(len(vectors))))]

    rng = np.random.default_rng(seed)
    centers = [X[int(np.argmax(w))]]
    while len(centers) < k:
        d = 1 - np.max(X @ np.stack(centers).T, axis=1)
        p = np.clip(d, 0, None) * w
        if p.sum() <= 0:
            break
        centers.append(X[int(rng.choice(len(X), p=p / p.sum()))])
    C = np.stack(centers)
    for _ in range(iters):
        assign = np.argmax(X @ C.T, axis=1)
        newC = []
        for j in range(len(C)):
            m = assign == j
            newC.append(unit((X[m] * w[m, None]).sum(0)) if m.any() else C[j])
        newC = np.stack(newC)
        if np.allclose(newC, C, atol=1e-4):
            break
        C = newC
    assign = np.argmax(X @ C.T, axis=1)
    total = float(w.sum())
    out = []
    for j in range(len(C)):
        idx = [int(i) for i in np.where(assign == j)[0]]
        if idx:
            out.append(Facet(C[j], float(w[idx].sum()) / total, idx))
    return sorted(out, key=lambda f: f.weight, reverse=True)


def affinity(vector, user_facets: list[Facet], taste=None) -> float:
    """How well an item fits a person: its best facet match, softened by facet size (a facet
    built from 5 % of someone's likes shouldn't count as much as their main taste), never
    below a fraction of the plain taste-vector match."""
    v = unit(vector)
    best = -1.0
    for f in user_facets:
        best = max(best, float(np.dot(v, f.centroid)) * (0.75 + 0.25 * f.weight ** 0.5))
    if taste is not None:
        best = max(best, 0.9 * float(np.dot(v, unit(taste))))
    return best


def group_affinity(per_member: list[float]) -> float:
    """Blend of average and least misery: a pick for two people should suit both, not
    delight one and bore the other."""
    if not per_member:
        return 0.0
    return 0.5 * (sum(per_member) / len(per_member)) + 0.5 * min(per_member)


def blend(parts: list[tuple[np.ndarray, float]]) -> np.ndarray:
    """Weighted sum of unit vectors, normalised (query vectors for seeded rows and moods)."""
    acc = None
    for v, w in parts:
        if v is None or w == 0:
            continue
        u = unit(v) * w
        acc = u if acc is None else acc + u
    return unit(acc) if acc is not None else None


def max_similarity(vector, others: list) -> float:
    if not others:
        return 0.0
    v = unit(vector)
    return float(max(np.dot(v, unit(o)) for o in others))


def mmr(order: list[int], vectors: list, relevance: list[float], limit: int, lam: float = 0.7) -> list[int]:
    """Maximal marginal relevance: each next pick trades relevance against similarity to what
    is already picked, so a row isn't eight near-identical sequels."""
    if not order:
        return []
    U = {i: unit(vectors[i]) for i in order}
    picked: list[int] = []
    rest = list(order)
    while rest and len(picked) < limit:
        best, best_val = None, -1e9
        for i in rest:
            sim = max((float(np.dot(U[i], U[j])) for j in picked), default=0.0)
            val = lam * relevance[i] - (1 - lam) * sim
            if val > best_val:
                best, best_val = i, val
        picked.append(best)
        rest.remove(best)
    return picked


def match_percent(aff: float) -> int:
    """Map a cosine-style affinity onto the "92 % match" scale. MiniLM similarities between
    related films sit around 0.3-0.7; 0.15 reads as a coin flip, 0.65+ as a near-certain hit."""
    return int(max(1, min(99, round(50 + (aff - 0.15) * 98))))
