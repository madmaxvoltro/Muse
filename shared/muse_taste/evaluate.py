"""Offline evaluation: does a change actually make recommendations better?

Protocol (temporal leave-future-out, the standard for recommenders, because random splits
leak the future: Ji et al., "A Critical Study on Data Leakage in Recommender System Offline
Evaluation", TOIS 2023):

  for each user: cut their history at the time of the last `holdout_frac` of liked titles;
  build signals + profile from events *before* the cut only; rank every catalog item of the
  domain they hadn't seen by then; relevant = titles first touched after the cut that ended
  up liked (final preference > threshold).

Metrics: Recall@K, nDCG@K, MRR and HitRate@K for accuracy; intra-list diversity, interest
calibration (KL, lower is better) and catalog coverage for the list as a whole.

Strategies compared:
  legacy        the original pipeline: one mean vector of raw per-event weights
  mean          one mean vector, but over the corrected per-title signals
  multi         multi-interest profile, interest feature only
  full          every ranker feature (tags, negatives, item-to-item, related graph...)
  full+rerank   full, then calibrated/MMR list selection (exploration off)
  full+evolution  full+rerank with taste evolution (trends, satiation, rediscovery, rhythms)
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .evolution import adjustment, analyze, apply_importance
from .profile import TasteProfile, build_profile, mean_taste_vector, normalise_sim, unit
from .ranking import CandidateInput, Scored, score_candidates
from .rerank import intra_list_diversity, select
from .retrieval import related_support
from .signals import SignalEvent, aggregate

LIKE_THRESHOLD = 0.5


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / min(len(relevant), k)


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 2) for i, key in enumerate(ranked[:k]) if key in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def mrr(ranked: list[str], relevant: set[str]) -> float:
    for i, key in enumerate(ranked):
        if key in relevant:
            return 1.0 / (i + 1)
    return 0.0


def calibration_kl(items: list[Scored], profile: TasteProfile, alpha: float = 0.02) -> float:
    K = len(profile.interests)
    if K == 0 or not items:
        return 0.0
    p = profile.importance_vector().astype(np.float64)
    p /= p.sum()
    q = np.zeros(K)
    for s in items:
        if s.membership.size == K:
            q += s.membership
    q /= max(q.sum(), 1e-12)
    q = (1 - alpha) * q + alpha * p
    m = p > 0
    return float((p[m] * np.log(p[m] / q[m])).sum())


@dataclass
class UserSplit:
    user_id: str
    train: list[SignalEvent]
    relevant: set[str]
    cutoff: datetime


def temporal_split(user_id: str, events: list[SignalEvent], holdout_frac: float = 0.2, now: datetime | None = None) -> UserSplit | None:
    now = now or datetime.now(timezone.utc)
    final = aggregate(events, now=now)
    first_touch: dict[str, datetime] = {}
    for e in events:
        if e.action == "cataloged":
            continue
        if e.key not in first_touch or e.timestamp < first_touch[e.key]:
            first_touch[e.key] = e.timestamp
    liked = sorted((first_touch[k], k) for k, s in final.items() if s.preference > LIKE_THRESHOLD and k in first_touch)
    if len(liked) < 5:
        return None
    n_hold = max(1, int(round(len(liked) * holdout_frac)))
    cutoff = liked[-n_hold][0]
    train = [e for e in events if e.timestamp < cutoff]
    relevant = {k for t, k in liked if t >= cutoff}
    if not train or not relevant:
        return None
    return UserSplit(user_id, train, relevant, cutoff)


def legacy_mean_vector(events: list[SignalEvent], vectors: dict[str, np.ndarray], now: datetime, efold_days: float = 60.0) -> np.ndarray | None:
    """Faithful to the pre-rewrite worker: every event's action_weight, exp decay, |w|-normalised."""
    acc = None
    tot = 0.0
    for e in events:
        if e.key not in vectors or e.action_weight == 0:
            continue
        age = max((now - e.timestamp).total_seconds() / 86400.0, 0.0)
        w = e.action_weight * math.exp(-age / efold_days)
        v = np.asarray(vectors[e.key], dtype=np.float64) * w
        acc = v if acc is None else acc + v
        tot += abs(w)
    return None if acc is None or tot == 0 else acc / tot


@dataclass
class EvalResult:
    per_strategy: dict[str, dict[str, float]] = field(default_factory=dict)
    users: int = 0

    def table(self) -> str:
        cols = ["recall@10", "ndcg@10", "mrr", "hit@10", "diversity", "calib_kl", "coverage"]
        lines = ["strategy        " + "  ".join(f"{c:>9}" for c in cols)]
        for name, m in self.per_strategy.items():
            lines.append(f"{name:<15} " + "  ".join(f"{m[c]:9.4f}" if c in m else f"{'-':>9}" for c in cols))
        return "\n".join(lines)


def evaluate(
    users: dict[str, list[SignalEvent]],
    catalog: dict[str, dict],
    vectors: dict[str, np.ndarray],
    tags: dict[str, list[str]] | None = None,
    tag_df: dict[str, int] | None = None,
    related_of: dict[str, list[str]] | None = None,
    titles: dict[str, str] | None = None,
    k: int = 10,
    holdout_frac: float = 0.2,
    strategies: tuple[str, ...] = ("legacy", "mean", "multi", "full", "full+rerank", "full+evolution"),
) -> EvalResult:
    """`catalog`: key -> {"item_type", "domain", "vote_average", "vote_count", "collection_id", "year"}."""
    tags = tags or {}
    related_of = related_of or {}
    titles = titles or {}
    sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    recommended: dict[str, set[str]] = defaultdict(set)
    n_users = 0
    keys_all = [key for key in catalog if key in vectors]
    for uid, events in users.items():
        split = temporal_split(uid, events, holdout_frac)
        if split is None:
            continue
        now = split.cutoff
        sig = aggregate(split.train, now=now)
        domains = {catalog[key]["domain"] for key in split.relevant if key in catalog}
        if not domains:
            continue
        n_users += 1
        for domain in domains:
            rel = {key for key in split.relevant if catalog.get(key, {}).get("domain") == domain}
            if not rel:
                continue
            dsig = {key: s for key, s in sig.items() if catalog.get(key, {}).get("domain") == domain}
            pool_keys = [key for key in keys_all if catalog[key]["domain"] == domain and not (key in dsig and dsig[key].seen)]
            if not pool_keys:
                continue
            P = unit(np.stack([np.asarray(vectors[key], dtype=np.float32) for key in pool_keys]))
            prof = build_profile(uid, domain, dsig, vectors, tags, tag_df, catalog_size=len(catalog), now=now)
            top = [(a.key, a.weight, titles.get(a.key, "")) for a in prof.anchors if a.weight > 0][:25]
            rel_support = related_support(top, related_of)
            weight_share = 1.0 / len(domains)

            for strat in strategies:
                if strat in ("legacy", "mean"):
                    v = legacy_mean_vector(split.train, vectors, now) if strat == "legacy" else mean_taste_vector(dsig, vectors, now)
                    if v is None:
                        continue
                    sims = P @ unit(v).astype(np.float32)
                    ranked = [pool_keys[i] for i in np.argsort(-sims)]
                    items_for_list: list[Scored] | None = None
                else:
                    cands = [
                        CandidateInput(
                            key=key,
                            vector=P[i],
                            tags=tags.get(key, []),
                            vote_average=catalog[key].get("vote_average"),
                            vote_count=catalog[key].get("vote_count"),
                            related_support=rel_support.get(key, (0.0, []))[0],
                            related_via=rel_support.get(key, (0.0, []))[1],
                            payload={"collection_id": catalog[key].get("collection_id"), "year": catalog[key].get("year")},
                        )
                        for i, key in enumerate(pool_keys)
                    ]
                    weights = {f: 0.0 for f in ("short_term", "item_sim", "tags", "negative", "related", "household", "intent", "quality", "freshness", "fatigue", "consensus")} if strat == "multi" else None
                    use_prof, adj = prof, None
                    if strat == "full+evolution":
                        use_prof = TasteProfile.from_json(prof.to_json())
                        dev = [e for e in split.train if catalog.get(e.key, {}).get("domain") == domain]
                        use_prof.evolution = analyze(use_prof, dev, vectors, now=now)
                        adj = adjustment(use_prof, at=now)
                        apply_importance(use_prof, adj)
                    scored = score_candidates(use_prof, cands, anchor_vectors=vectors, weights=weights, evolution=adj)
                    scored.sort(key=lambda s: -s.score)
                    if strat in ("full+rerank", "full+evolution"):
                        head = select(scored[: max(200, k * 10)], use_prof, limit=k, explore_ratio=0.0)
                        head_keys = [s.key for s in head]
                        ranked = head_keys + [s.key for s in scored if s.key not in set(head_keys)]
                        items_for_list = head
                    else:
                        ranked = [s.key for s in scored]
                        items_for_list = scored[:k]
                m = sums[strat]
                m["recall@10"] += recall_at_k(ranked, rel, k) * weight_share
                m["ndcg@10"] += ndcg_at_k(ranked, rel, k) * weight_share
                m["mrr"] += mrr(ranked, rel) * weight_share
                m["hit@10"] += float(bool(set(ranked[:k]) & rel)) * weight_share
                if items_for_list is not None:
                    m["diversity"] += intra_list_diversity(items_for_list, prof.baseline) * weight_share
                    m["calib_kl"] += calibration_kl(items_for_list, prof) * weight_share  # vs. the unadjusted profile
                else:
                    idx = [pool_keys.index(x) for x in ranked[:k]]
                    S = normalise_sim(P[idx] @ P[idx].T, prof.baseline)
                    m["diversity"] += float(1.0 - (S.sum() - np.trace(S)) / (k * (k - 1))) * weight_share
                recommended[strat].update(ranked[:k])
    res = EvalResult(users=n_users)
    for strat, m in sums.items():
        res.per_strategy[strat] = {key: v / max(n_users, 1) for key, v in m.items()}
        res.per_strategy[strat]["coverage"] = len(recommended[strat]) / max(len(keys_all), 1)
    return res
