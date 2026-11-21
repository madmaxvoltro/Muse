"""Multi-interest taste profiles.

One averaged taste vector is the classic failure of embedding recommenders: someone who loves
both slow Nordic crime and Studio Ghibli gets a vector halfway between, which is close to
*neither*, and recommendations become a beige blur of whatever sits in the middle.

Following PinnerSage (Pal et al., "PinnerSage: Multi-Modal User Embedding Framework for
Recommendations at Pinterest", KDD 2020) a user is represented by several interests:

1. Positive items are clustered with Ward hierarchical agglomerative clustering. The number of
   clusters adapts to the data: we cut the dendrogram at the smallest k whose clusters are
   coherent, so a light user gets 1-3 interests and a heavy one 10+.
2. Each interest is summarised by its **medoid** (the most central real item, robust to
   outliers and never drifting into empty space between clusters) and its weighted centroid.
3. **Importance** = sum of decayed preference weights. It drives how many retrieval slots an
   interest gets and the target proportions for calibration (rerank.py).

Plus:
* **Negative prototypes**: disliked items kept as individual repulsors, not averaged into the
  positive vector, where they only tilt the average. Wang et al., "Learning from Negative
  User Feedback and Measuring Responsiveness for Sequential Recommenders" (RecSys 2023) show
  negatives need their own "not-to-recommend" objective; our analogue is a repulsion term.
* **Short-term intent**: what you have been into the last two weeks, kept separate from
  long-term taste.
* **Tag affinities**: IDF-weighted, shrunk preference per exact tag (director, keyword...).
* **Baseline**: the model's typical cosine between unrelated items. Embedding spaces are
  anisotropic and every model has a different "zero"; similarities are normalised against
  it so ranking weights mean the same thing whichever model is configured.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from .documents import TAG_NAMESPACE_WEIGHT, tag_namespace
from .signals import ItemSignal

MAX_INTERESTS = 16
MAX_ITEMS_FOR_CLUSTERING = 1500
MAX_NEGATIVES = 64
MAX_ANCHORS = 400
COHERENCE_TARGET = 0.42  # fraction of the gap between baseline and 1.0 a cluster must close
SHORT_TERM_DAYS = 14
SHORT_TERM_HALF_LIFE_DAYS = 3.0
MAX_TAGS = 400


def unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n == 0, 1.0, n)


@dataclass
class Interest:
    id: int
    kind: str  # "behaviour" | "pinned"
    centroid: list[float]
    medoid: list[float]
    medoid_key: str
    importance: float
    size: int
    members: list[str]  # top member keys by weight
    top_tags: list[str] = field(default_factory=list)
    label: str | None = None
    # Typical (25th percentile) normalised similarity of members to the centroid: where the
    # interest's edge is. Titles just beyond it are "one step further" (evolution.stretch).
    radius: float = 0.6


@dataclass
class Prototype:
    key: str
    vector: list[float]
    weight: float  # positive number: strength of the dislike


@dataclass
class Anchor:
    key: str
    preference: float
    confidence: float
    weight: float


@dataclass
class TasteProfile:
    user_id: str
    domain: str
    computed_at: str
    baseline: float
    interests: list[Interest]
    negatives: list[Prototype]
    short_term: list[float] | None
    tag_weights: dict[str, float]
    tag_scale: float
    anchors: list[Anchor]  # strongest judged items, for uncertainty + item-to-item retrieval
    seen: list[str]
    in_progress: list[str]
    backlog: dict[str, float]  # intent strength for want-to-watch items
    abandoned: list[str]  # stopped early, reason unknown -> ask why
    n_positive: int
    n_negative: int
    total_confidence: float
    related: list[dict] = field(default_factory=list)  # filled by the worker (TMDB graph)
    household: list[dict] = field(default_factory=list)  # filled by the worker (other profiles)
    summary: str | None = None  # optional LLM natural-language summary
    model: str | None = None
    evolution: dict = field(default_factory=dict)  # muse_taste.evolution.analyze() output
    reconsume: list = field(default_factory=list)  # muse_taste.reconsume.candidates(): worth watching/hearing again
    muted: list = field(default_factory=list)  # titles you said not to base recommendations on (scrutability)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        _round_vectors(d)
        return d

    @staticmethod
    def from_json(d: dict[str, Any]) -> "TasteProfile":
        d = dict(d)
        d["interests"] = [Interest(**i) for i in d.get("interests", [])]
        d["negatives"] = [Prototype(**p) for p in d.get("negatives", [])]
        d["anchors"] = [Anchor(**a) for a in d.get("anchors", [])]
        known = TasteProfile.__dataclass_fields__.keys()
        return TasteProfile(**{k: v for k, v in d.items() if k in known})

    # ---- numpy views -----------------------------------------------------------------
    def centroid_matrix(self) -> np.ndarray:
        return np.array([i.centroid for i in self.interests], dtype=np.float32) if self.interests else np.zeros((0, 0), np.float32)

    def medoid_matrix(self) -> np.ndarray:
        return np.array([i.medoid for i in self.interests], dtype=np.float32) if self.interests else np.zeros((0, 0), np.float32)

    def importance_vector(self) -> np.ndarray:
        return np.array([i.importance for i in self.interests], dtype=np.float32)

    def negative_matrix(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.negatives:
            return np.zeros((0, 0), np.float32), np.zeros(0, np.float32)
        return (
            np.array([p.vector for p in self.negatives], dtype=np.float32),
            np.array([p.weight for p in self.negatives], dtype=np.float32),
        )


def _round_vectors(d: Any) -> None:
    if isinstance(d, dict):
        for k, v in d.items():
            if k in ("centroid", "medoid", "vector", "short_term") and isinstance(v, list):
                d[k] = [round(float(x), 5) for x in v]
            else:
                _round_vectors(v)
    elif isinstance(d, list):
        for x in d:
            _round_vectors(x)


def estimate_baseline(vectors: np.ndarray, rng: np.random.Generator | None = None, samples: int = 4000) -> float:
    """Mean cosine between random pairs: the model's 'unrelated' level. Clipped to [0, 0.8]."""
    n = len(vectors)
    if n < 3:
        return 0.2
    rng = rng or np.random.default_rng(0)
    a = rng.integers(0, n, samples)
    b = rng.integers(0, n, samples)
    mask = a != b
    if not mask.any():
        return 0.2
    v = unit(vectors)
    sims = np.einsum("ij,ij->i", v[a[mask]], v[b[mask]])
    return float(np.clip(np.mean(sims), 0.0, 0.8))


def normalise_sim(sim: np.ndarray | float, baseline: float) -> np.ndarray | float:
    """Maps raw cosine so the baseline is 0 and identical is 1."""
    return (sim - baseline) / max(1.0 - baseline, 1e-6)


def cluster_items(vectors: np.ndarray, baseline: float, max_k: int = MAX_INTERESTS, weights: np.ndarray | None = None) -> np.ndarray:
    """Ward clustering with an adaptive cut: the smallest k whose weighted mean
    (item -> own centroid) normalised similarity reaches COHERENCE_TARGET."""
    n = len(vectors)
    if n == 1:
        return np.zeros(1, dtype=int)
    if n == 2:
        sim = float(unit(vectors[0]) @ unit(vectors[1]))
        return np.array([0, 0]) if normalise_sim(sim, baseline) >= COHERENCE_TARGET else np.array([0, 1])
    from scipy.cluster.hierarchy import fcluster, linkage

    v = unit(vectors.astype(np.float64))
    z = linkage(v, method="ward")
    w = np.ones(n) if weights is None else np.maximum(weights, 1e-6)
    upper = min(max_k, n)
    labels = np.zeros(n, dtype=int)
    for k in range(1, upper + 1):
        labels = fcluster(z, k, criterion="maxclust") - 1
        coh = 0.0
        for c in np.unique(labels):
            idx = labels == c
            cen = unit((v[idx] * w[idx, None]).sum(0))
            coh += float((normalise_sim(v[idx] @ cen, baseline) * w[idx]).sum())
        coh /= float(w.sum())
        if coh >= COHERENCE_TARGET:
            break
    return labels


def _weighted_medoid(v: np.ndarray, w: np.ndarray) -> int:
    sims = v @ v.T
    return int(np.argmax(sims @ w))


def build_profile(
    user_id: str,
    domain: str,
    signals: dict[str, ItemSignal],
    vectors: dict[str, np.ndarray],
    tags: dict[str, list[str]] | None = None,
    tag_df: dict[str, int] | None = None,
    catalog_size: int = 1000,
    baseline: float | None = None,
    now: datetime | None = None,
    pinned: list[tuple[str, np.ndarray]] | None = None,
    model: str | None = None,
    muted: set[str] | None = None,
    tag_overrides: dict[str, str] | None = None,
) -> TasteProfile:
    """Pure function: signals + item vectors in, profile out. No I/O, so the recommendation
    API, the offline evaluator and the tests all run exactly this code."""
    now = now or datetime.now(timezone.utc)
    tags = tags or {}
    tag_df = tag_df or {}

    muted = muted or set()
    pos: list[tuple[str, float, ItemSignal]] = []
    neg: list[tuple[str, float, ItemSignal]] = []
    for key, s in signals.items():
        w = s.weight(now)
        if key not in vectors or w == 0 or key in muted:  # "don't base recommendations on this"
            continue
        (pos if w > 0 else neg).append((key, w, s))
    pos.sort(key=lambda t: -t[1])
    neg.sort(key=lambda t: t[1])
    pos_c = pos[:MAX_ITEMS_FOR_CLUSTERING]

    if baseline is None:
        all_vecs = np.array([vectors[k] for k, _, _ in pos + neg], dtype=np.float32) if pos or neg else np.zeros((0, 1))
        baseline = estimate_baseline(all_vecs) if len(all_vecs) >= 10 else 0.2

    interests: list[Interest] = []
    if pos_c:
        keys = [k for k, _, _ in pos_c]
        w = np.array([wt for _, wt, _ in pos_c], dtype=np.float64)
        v = unit(np.array([vectors[k] for k in keys], dtype=np.float64))
        # Structure from undecayed preference, importance from decayed weight: an interest you
        # loved two years ago is still its own interest (dormant, small importance), not
        # swallowed by whatever you watch now. evolution.py needs it to exist to bring it back.
        pref_w = np.array([max(sig.preference, 0.05) for _, _, sig in pos_c], dtype=np.float64)
        labels = cluster_items(v, baseline, weights=pref_w)
        total = float(w.sum())
        for c in np.unique(labels):
            idx = np.where(labels == c)[0]
            cw = w[idx]
            cv = v[idx]
            cen = unit((cv * cw[:, None]).sum(0))
            mi = idx[_weighted_medoid(cv, cw)]
            order = idx[np.argsort(-cw)]
            member_sims = normalise_sim(cv @ cen, baseline)
            radius = float(np.clip(np.percentile(member_sims, 25), 0.2, 0.9)) if len(idx) >= 3 else 0.6
            interests.append(
                Interest(
                    id=0,
                    kind="behaviour",
                    centroid=cen.tolist(),
                    medoid=v[mi].tolist(),
                    medoid_key=keys[mi],
                    importance=float(cw.sum() / total),
                    size=int(len(idx)),
                    members=[keys[i] for i in order[:12]],
                    top_tags=_top_tags([keys[i] for i in idx], cw, tags, tag_df, catalog_size),
                    radius=radius,
                )
            )
    if pinned:
        # A pinned topic ("space opera") becomes an interest of its own with a fixed share,
        # so it gets retrieval slots and a calibration target without touching behaviour.
        share = min(0.15 * len(pinned), 0.35) if interests else 1.0
        for i in interests:
            i.importance *= 1.0 - share
        for text, vec in pinned:
            u = unit(np.asarray(vec, dtype=np.float64))
            interests.append(
                Interest(id=0, kind="pinned", centroid=u.tolist(), medoid=u.tolist(), medoid_key=f"pinned:{text}",
                         importance=share / len(pinned), size=0, members=[], label=text)
            )
    interests.sort(key=lambda i: -i.importance)
    for n, i in enumerate(interests):
        i.id = n

    negatives = [
        Prototype(key=k, vector=unit(np.asarray(vectors[k], dtype=np.float64)).tolist(), weight=float(min(-wt, 2.0)))
        for k, wt, _ in neg[:MAX_NEGATIVES]
    ]

    # short-term intent: recent positives, fast decay
    st_cut = now - timedelta(days=SHORT_TERM_DAYS)
    st = [(k, s) for k, _, s in pos if s.last_ts and s.last_ts >= st_cut]
    short_term = None
    if len(st) >= 2:
        sv = np.array([vectors[k] for k, _ in st], dtype=np.float64)
        sw = np.array(
            [s.preference * 0.5 ** ((now - s.last_ts).total_seconds() / 86400.0 / SHORT_TERM_HALF_LIFE_DAYS) for _, s in st]
        )
        short_term = unit((unit(sv) * sw[:, None]).sum(0)).tolist()

    tag_weights, tag_scale = _tag_affinities(pos + neg, tags, tag_df, catalog_size)
    tag_weights = apply_tag_overrides(tag_weights, tag_scale, tag_overrides or {})

    judged = sorted(
        ((k, s) for k, s in signals.items() if k in vectors and s.weight(now) != 0 and k not in muted),
        key=lambda t: -abs(t[1].weight(now)) * (1 + math.log1p(t[1].confidence)),
    )[:MAX_ANCHORS]
    anchors = [Anchor(key=k, preference=s.preference, confidence=s.confidence, weight=s.weight(now)) for k, s in judged]

    return TasteProfile(
        user_id=user_id,
        domain=domain,
        computed_at=now.isoformat(),
        baseline=float(baseline),
        interests=interests,
        negatives=negatives,
        short_term=short_term,
        tag_weights=tag_weights,
        tag_scale=tag_scale,
        anchors=anchors,
        seen=sorted(k for k, s in signals.items() if s.seen),
        in_progress=sorted(k for k, s in signals.items() if s.in_progress),
        backlog={k: s.intent for k, s in signals.items() if s.intent > 0 and not s.seen},
        abandoned=sorted(k for k, s in signals.items() if s.abandoned),
        n_positive=len(pos),
        n_negative=len(neg),
        total_confidence=float(sum(s.confidence for s in signals.values())),
        model=model,
        muted=sorted(muted),
    )


def apply_tag_overrides(weights: dict[str, float], scale: float, overrides: dict[str, str]) -> dict[str, float]:
    """Your own word beats inference: 'more' makes a tag a clear positive, 'less' a clear negative."""
    out = dict(weights)
    for tag, response in overrides.items():
        if response == "more":
            out[tag] = max(out.get(tag, 0.0), 0.6 * scale)
        elif response == "less":
            out[tag] = min(out.get(tag, 0.0), -0.6 * scale)
    return out


def _idf(tag: str, tag_df: dict[str, int], n: int) -> float:
    return math.log((n + 1) / (tag_df.get(tag, 1) + 1)) + 1.0


def _tag_affinities(items: list[tuple[str, float, ItemSignal]], tags: dict[str, list[str]], tag_df: dict[str, int], n: int) -> tuple[dict[str, float], float]:
    """Per tag: shrunk mean preference x support x IDF x namespace weight. The +1 in the
    denominator shrinks one-off tags toward zero (one liked film doesn't make an actor a
    favourite); log1p(count) rewards repeated evidence."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for key, w, _ in items:
        for t in tags.get(key, []):
            sums[t] = sums.get(t, 0.0) + w
            counts[t] = counts.get(t, 0) + 1
    out: dict[str, float] = {}
    for t, s in sums.items():
        c = counts[t]
        val = s / (c + 1.0) * math.log1p(c) * _idf(t, tag_df, n) * TAG_NAMESPACE_WEIGHT.get(tag_namespace(t), 0.7)
        if abs(val) > 1e-4:
            out[t] = round(val, 4)
    top = dict(sorted(out.items(), key=lambda kv: -abs(kv[1]))[:MAX_TAGS])
    vals = np.abs(np.array(list(top.values()))) if top else np.array([1.0])
    scale = float(np.percentile(vals, 90)) * 2.0 if len(vals) else 1.0
    return top, max(scale, 1e-3)


def _top_tags(keys: list[str], w: np.ndarray, tags: dict[str, list[str]], tag_df: dict[str, int], n: int, k: int = 6) -> list[str]:
    score: dict[str, float] = {}
    for key, wt in zip(keys, w):
        for t in tags.get(key, []):
            if tag_namespace(t) in ("decade", "lang"):
                continue
            score[t] = score.get(t, 0.0) + float(wt) * _idf(t, tag_df, n)
    return [t for t, _ in sorted(score.items(), key=lambda kv: -kv[1])[:k]]


def mean_taste_vector(signals: dict[str, ItemSignal], vectors: dict[str, np.ndarray], now: datetime | None = None) -> np.ndarray | None:
    """Single positive-weighted mean, kept for the legacy `taste_vectors` collection (curator
    fallback) and as the evaluation baseline."""
    now = now or datetime.now(timezone.utc)
    acc = None
    for k, s in signals.items():
        w = s.weight(now)
        if w <= 0 or k not in vectors:
            continue
        v = unit(np.asarray(vectors[k], dtype=np.float64)) * w
        acc = v if acc is None else acc + v
    return None if acc is None else unit(acc)
