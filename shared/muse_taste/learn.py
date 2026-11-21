"""Re-fitting the ranker's weights from what actually happened to shown recommendations.

Every served list is logged as impressions (recommendation-api). An impression is labelled
positive if the title was then played, finished or thumbed up within a week, and strongly
negative on a thumbs down or "not for me". Shown repeatedly and never touched counts as a
weak negative.

The model is a logistic regression over the *same named features* the hand-set ranker uses,
so explanations keep working. With a household's worth of data an unconstrained fit would
overfit badly, so it's MAP estimation with a Gaussian prior centred on the hand-set weights:
little data keeps them, lots of consistent data moves them. The learned weights are adopted
only if they beat the hand-set ones on a later, held-out slice of time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

MIN_LABELS = 200
MIN_PER_CLASS = 20
LABEL_WINDOW = timedelta(days=7)
_POSITIVE = {"completed", "favorited", "chose", "added_watchlist", "requested"}
_STRONG_NEGATIVE_REASONS = {"not_for_me", "too_slow"}


def label_impression(shown_at: datetime, events: list) -> tuple[float, float]:
    """What happened to a shown title within a week -> (label, sample weight).
    events: SignalEvents for that title. Thumbs and "not for me" are decisive; playing or
    finishing it is positive; ignored is a weak negative."""
    later = [e for e in events if shown_at <= e.timestamp <= shown_at + LABEL_WINDOW]
    for e in later:
        if e.action == "rated":
            v = e.metadata.get("value", 1 if e.action_weight > 0 else -1)
            return (1.0, 1.0) if v and v > 0 else (0.0, 1.0)
        if e.action == "rejected" or (e.action == "abandon_reason" and e.metadata.get("reason") in _STRONG_NEGATIVE_REASONS):
            return 0.0, 1.0
    for e in later:
        if e.action in _POSITIVE or (e.action == "played" and (e.progress_pct or 0) >= 20):
            return 1.0, 1.0
    return 0.0, 0.3


@dataclass
class FitResult:
    weights: dict[str, float]
    adopted: bool
    auc_default: float
    auc_learned: float
    n_train: int
    n_valid: int
    note: str


def auc(scores: np.ndarray, labels: np.ndarray, weights: np.ndarray | None = None) -> float:
    """Weighted ROC AUC (probability a random positive outranks a random negative)."""
    w = np.ones_like(scores, dtype=np.float64) if weights is None else weights.astype(np.float64)
    pos = labels > 0.5
    if not pos.any() or pos.all():
        return 0.5
    order = np.argsort(scores, kind="mergesort")
    s, l, ww = scores[order], pos[order], w[order]
    # ties: average rank handled by grouping
    cum_neg = 0.0
    total = 0.0
    i = 0
    n = len(s)
    while i < n:
        j = i
        while j + 1 < n and s[j + 1] == s[i]:
            j += 1
        grp_pos = ww[i : j + 1][l[i : j + 1]].sum()
        grp_neg = ww[i : j + 1][~l[i : j + 1]].sum()
        total += grp_pos * (cum_neg + 0.5 * grp_neg)
        cum_neg += grp_neg
        i = j + 1
    return float(total / (ww[pos].sum() * ww[~pos].sum()))


def _fit_logistic(X: np.ndarray, y: np.ndarray, sw: np.ndarray, prior: np.ndarray, l2: float, iters: int = 60) -> tuple[np.ndarray, float]:
    """Newton-Raphson MAP fit; bias unregularised."""
    n, f = X.shape
    w = prior.copy()
    base = float(np.clip((y * sw).sum() / sw.sum(), 1e-3, 1 - 1e-3))
    b = float(np.log(base / (1 - base)) - (X @ w).mean())
    Xb = np.hstack([X, np.ones((n, 1))])
    theta = np.append(w, b)
    reg = np.full(f + 1, l2)
    reg[-1] = 0.0
    center = np.append(prior, 0.0)
    for _ in range(iters):
        z = Xb @ theta
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = Xb.T @ (sw * (p - y)) + reg * (theta - center)
        H = (Xb * (sw * p * (1 - p))[:, None]).T @ Xb + np.diag(reg + 1e-6)
        step = np.linalg.solve(H, g)
        theta -= step
        if np.abs(step).max() < 1e-6:
            break
    return theta[:-1], float(theta[-1])


def fit_weights(
    feature_names: list[str],
    X: np.ndarray,
    y: np.ndarray,
    sample_weight: np.ndarray,
    time_order: np.ndarray,
    default: dict[str, float],
    l2: float = 4.0,
    valid_frac: float = 0.25,
) -> FitResult:
    n = len(y)
    w0 = np.array([default.get(f, 0.0) for f in feature_names])
    if n < MIN_LABELS or (y > 0.5).sum() < MIN_PER_CLASS or (y <= 0.5).sum() < MIN_PER_CLASS:
        return FitResult(dict(default), False, 0.5, 0.5, n, 0, f"not enough labelled impressions yet ({n})")
    order = np.argsort(time_order)
    cut = int(n * (1 - valid_frac))
    tr, va = order[:cut], order[cut:]

    # Put the hand-set weights on the logit scale first (one scalar), so the prior is
    # centred on "the current ranker, calibrated", then fit everything around it.
    s0 = X[tr] @ w0
    a_theta, _ = _fit_logistic(s0[:, None], y[tr], sample_weight[tr], np.array([1.0]), l2=0.0)
    a = float(max(a_theta[0], 0.05))
    w_logit, _ = _fit_logistic(X[tr], y[tr], sample_weight[tr], a * w0, l2=l2)
    learned = w_logit / a  # back to score units, so sigma / explore keep their meaning

    auc_d = auc(X[va] @ w0, y[va], sample_weight[va])
    auc_l = auc(X[va] @ learned, y[va], sample_weight[va])
    adopted = auc_l > auc_d + 0.01
    weights = {f: float(round(v, 4)) for f, v in zip(feature_names, learned)} if adopted else dict(default)
    for k, v in default.items():
        weights.setdefault(k, v)
    # sign constraints: never learn to *like* disliked-similarity or fatigue
    for k in ("negative", "fatigue"):
        if weights.get(k, 0) > 0:
            weights[k] = default[k]
    return FitResult(weights, adopted, auc_d, auc_l, len(tr), len(va), "adopted" if adopted else "kept hand-set weights (no held-out gain)")
