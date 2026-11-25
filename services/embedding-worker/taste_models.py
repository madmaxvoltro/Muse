"""The maths behind "understanding taste" — pure numpy (scipy for the sparse SVD).

* weighted_kmeans   — several taste facets instead of one average (thrash + ballads is two
                      tastes, not one grey middle).
* behavior_embeddings — item vectors learnt from what is played *together* (sessions), via
                      PPMI + truncated SVD: relations no tag or description contains.
* PreferenceModel   — logistic regression on a person's own likes vs skips over audio features:
                      what they pick *within* a genre (energy yes, harsh brightness no).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np


# --- facets -------------------------------------------------------------------------------------


def _unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    return m / np.where(n == 0, 1, n)


def choose_k(n_items: int, max_k: int = 6) -> int:
    return int(max(1, min(max_k, n_items // 12)))


def weighted_kmeans(x: np.ndarray, w: np.ndarray, k: int, iters: int = 30, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Spherical k-means with item weights (k-means++ init). Returns (centroids, labels)."""
    x = _unit(np.asarray(x, dtype=np.float64))
    w = np.asarray(w, dtype=np.float64)
    n = len(x)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    centers = [x[int(np.argmax(w))]]
    for _ in range(1, k):
        d = 1 - np.max(x @ np.array(centers).T, axis=1)
        p = np.maximum(d, 0) * w
        if p.sum() <= 0:
            break
        centers.append(x[rng.choice(n, p=p / p.sum())])
    c = np.array(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        new = np.argmax(x @ c.T, axis=1)
        if _ > 0 and np.array_equal(new, labels):
            break
        labels = new
        for j in range(len(c)):
            m = labels == j
            if m.any():
                c[j] = (x[m] * w[m, None]).sum(axis=0)
        c = _unit(c)
    return c, labels


@dataclass
class Facet:
    centroid: np.ndarray
    members: list[int]
    share: float  # share of the person's positive weight


def _quality(x: np.ndarray, w: np.ndarray, c: np.ndarray, labels: np.ndarray) -> float:
    """Weighted mean cosine of items to their own centroid."""
    xu = _unit(x)
    return float((np.sum(xu * c[labels], axis=1) * w).sum() / (w.sum() or 1.0))


def facets(x: np.ndarray, w: np.ndarray, max_k: int = 6, min_share: float = 0.06, min_gain: float = 0.04) -> list[Facet]:
    """Taste facets from positively weighted items. k grows while a facet more makes items
    clearly closer to their centre (elbow on weighted cohesion); tiny facets are noise."""
    if len(x) == 0:
        return []
    x = np.asarray(x, dtype=np.float64)
    w = np.asarray(w, dtype=np.float64)
    upper = choose_k(len(x), max_k)

    def best_of(k: int) -> tuple[np.ndarray, np.ndarray, float]:
        runs = [weighted_kmeans(x, w, k, seed=s) for s in (7, 11, 23)]
        scored = [(_quality(x, w, c_, l_), c_, l_) for c_, l_ in runs]
        q_, c_, l_ = max(scored, key=lambda t: t[0])
        return c_, l_, q_

    c, labels, q = best_of(1)
    for k in range(2, upper + 1):
        c2, l2, q2 = best_of(k)
        if q2 - q < min_gain:
            break
        c, labels, q = c2, l2, q2
    total = float(w.sum()) or 1.0
    out = []
    for j in range(len(c)):
        idx = [i for i in range(len(labels)) if labels[i] == j]
        share = float(w[idx].sum()) / total if idx else 0.0
        if idx and share >= min_share:
            out.append(Facet(c[j], sorted(idx, key=lambda i: -w[i]), share))
    return sorted(out, key=lambda f: -f.share)


# --- behaviour --------------------------------------------------------------------------------------


def sessions(events: list[tuple[str, str, float, str]], gap_s: float = 1800) -> list[list[str]]:
    """events: (person, item_key, unix time, action) sorted by person, time. A session is a
    run of positive plays with no gap > gap_s; items repeat at most once in a row."""
    out: list[list[str]] = []
    cur: list[str] = []
    last_p, last_t = None, 0.0
    for person, item, t, action in events:
        if action not in ("completed", "played", "favorited", "added_to_playlist", "rated"):
            continue
        if person != last_p or t - last_t > gap_s:
            if len(cur) > 1:
                out.append(cur)
            cur = []
        if not cur or cur[-1] != item:
            cur.append(item)
        last_p, last_t = person, t
    if len(cur) > 1:
        out.append(cur)
    return out


def behavior_embeddings(seqs: list[list[str]], dim: int = 64, window: int = 5, min_count: int = 2) -> dict[str, np.ndarray]:
    """PPMI over windowed co-occurrence, factorised with truncated SVD (what word2vec
    implicitly does). Needs a reasonable amount of listening; returns {} otherwise."""
    counts: dict[str, int] = defaultdict(int)
    for s in seqs:
        for it in s:
            counts[it] += 1
    vocab = [it for it, c in counts.items() if c >= min_count]
    if len(vocab) < 20:
        return {}
    index = {it: i for i, it in enumerate(vocab)}
    co: dict[tuple[int, int], float] = defaultdict(float)
    for s in seqs:
        ids = [index[it] for it in s if it in index]
        for i, a in enumerate(ids):
            for j in range(i + 1, min(len(ids), i + 1 + window)):
                b = ids[j]
                if a != b:
                    wgt = 1.0 / (j - i)
                    co[(a, b)] += wgt
                    co[(b, a)] += wgt
    if not co:
        return {}
    from scipy.sparse import csr_matrix
    from scipy.sparse.linalg import svds

    n = len(vocab)
    rows, cols = zip(*co.keys())
    vals = np.array(list(co.values()))
    m = csr_matrix((vals, (rows, cols)), shape=(n, n))
    total = vals.sum()
    row_sum = np.asarray(m.sum(axis=1)).ravel()
    col_sum = np.asarray(m.sum(axis=0)).ravel() ** 0.75  # context smoothing, as in word2vec
    col_sum = col_sum / col_sum.sum() * total
    m = m.tocoo()
    pmi = np.log((m.data * total) / (row_sum[m.row] * col_sum[m.col] + 1e-12) + 1e-12)
    keep = pmi > 0
    ppmi = csr_matrix((pmi[keep], (m.row[keep], m.col[keep])), shape=(n, n))
    k = int(min(dim, n - 2))
    if k < 2:
        return {}
    u, s, _ = svds(ppmi.astype(np.float64), k=k)
    vecs = _unit(u * np.sqrt(s))
    if vecs.shape[1] < dim:
        vecs = np.hstack([vecs, np.zeros((n, dim - vecs.shape[1]))])
    return {it: vecs[i] for it, i in index.items()}


def transitions(events: list[tuple[str, str, float, str]], gap_s: float = 1800) -> dict[tuple[str, str], dict[str, int]]:
    """A -> B within a session: how often B followed A, and how B ended (completed/skipped)."""
    out: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"count": 0, "completions": 0, "skips": 0})
    prev_item, prev_p, prev_t = None, None, 0.0
    for person, item, t, action in events:
        if person != prev_p or t - prev_t > gap_s:
            prev_item = None
        if prev_item and prev_item != item and action in ("played", "completed", "skipped"):
            d = out[(prev_item, item)]
            if action == "played":
                d["count"] += 1
            elif action == "completed":
                d["completions"] += 1
            else:
                d["skips"] += 1
        if action in ("played", "completed"):
            prev_item = item
        prev_p, prev_t = person, t
    return dict(out)


# --- preference model --------------------------------------------------------------------------------

from muse_schema.audio import PREF_FEATURES
from muse_schema.audio import feature_vector as _fv


def feature_vector(f: dict) -> np.ndarray | None:
    v = _fv(f)
    return None if v is None else np.array(v, dtype=np.float64)


@dataclass
class PreferenceModel:
    weights: np.ndarray
    bias: float
    mean: np.ndarray
    std: np.ndarray
    metrics: dict = field(default_factory=dict)

    def prob(self, x: np.ndarray) -> float:
        z = float(((x - self.mean) / self.std) @ self.weights + self.bias)
        return 1 / (1 + math.exp(-max(-30, min(30, z))))

    def to_json(self) -> dict:
        return {"features": list(PREF_FEATURES), "weights": self.weights.round(4).tolist(), "bias": round(self.bias, 4),
                "mean": self.mean.round(4).tolist(), "std": self.std.round(4).tolist()}

    @classmethod
    def from_json(cls, d: dict, metrics: dict | None = None) -> "PreferenceModel":
        return cls(np.array(d["weights"]), float(d["bias"]), np.array(d["mean"]), np.array(d["std"]), metrics or {})

    def describe(self) -> list[str]:
        """Plain-language reading of the strongest weights."""
        words = {
            "energy": ("high-energy", "calm, low-energy"), "danceability": ("steady, danceable grooves", "loose, less beat-driven music"),
            "acousticness": ("acoustic, organic sound", "electronic, produced sound"), "brightness": ("bright, crisp sound", "warm, dark sound"),
            "percussiveness": ("drum-heavy, percussive tracks", "smooth, less percussive tracks"), "dynamics": ("dynamic tracks that build and drop", "even, steady loudness"),
            "tempo": ("faster tempos", "slower tempos"), "major": ("major-key, brighter moods", "minor-key, darker moods"),
            "loudness": ("loud, dense masters", "quieter, spacious masters"),
        }
        order = np.argsort(-np.abs(self.weights))
        top = float(np.abs(self.weights).max(initial=0.0))
        out = []
        if self.metrics.get("auc", 1.0) < 0.6:
            return out  # the model can't tell likes from skips: say nothing rather than guess
        for i in order[:3]:
            if abs(self.weights[i]) < max(0.3, 0.45 * top):
                break
            pos, neg = words[PREF_FEATURES[i]]
            out.append(f"prefers {pos}" if self.weights[i] > 0 else f"prefers {neg}")
        return out


def _train(x: np.ndarray, y: np.ndarray, sw: np.ndarray, l2: float, iters: int, lr: float) -> PreferenceModel:
    mean, std = x.mean(axis=0), x.std(axis=0)
    std = np.where(std < 1e-6, 1.0, std)
    xs = (x - mean) / std
    w, b = np.zeros(x.shape[1]), 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(xs @ w + b)))
        g = (p - y) * sw
        w -= lr * (xs.T @ g / sw.sum() + l2 * w / len(y))
        b -= lr * g.sum() / sw.sum()
    return PreferenceModel(w, b, mean, std)


def fit_preference(x: np.ndarray, y: np.ndarray, sample_w: np.ndarray | None = None, l2: float = 8.0,
                   iters: int = 400, lr: float = 0.3) -> PreferenceModel | None:
    """L2-regularised logistic regression (gradient descent). Needs both classes, >= 30 rows.
    metrics.auc is measured on a held-out quarter, so a model that only memorised says so."""
    if len(x) < 30 or y.min() == y.max():
        return None
    sw = np.ones(len(y)) if sample_w is None else sample_w
    order = np.random.default_rng(3).permutation(len(y))
    cut = int(len(y) * 0.75)
    tr, te = order[:cut], order[cut:]
    held = None
    if y[tr].min() != y[tr].max() and y[te].min() != y[te].max():
        probe = _train(x[tr], y[tr], sw[tr], l2, iters, lr)
        held = auc(y[te], np.array([probe.prob(r) for r in x[te]]))
    model = _train(x, y, sw, l2, iters, lr)
    model.metrics = {"auc": round(held, 3) if held is not None else 0.5, "samples": int(len(y)),
                     "positive_share": round(float(y.mean()), 3)}
    return model


def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Rank-based AUC (probability a random positive outranks a random negative)."""
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))
