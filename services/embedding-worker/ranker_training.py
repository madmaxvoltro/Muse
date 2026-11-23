"""Learning from what was shown.

1. Label impressions: positive when the person (any of their linked ids) played to the end,
   favourited, rated up or added the item within 7 days of seeing it; negative when they
   skipped/rejected/rated it down; 'none' when 7 days passed without anything.
2. Train a logistic-regression ranker over the same features the engine scores with, and
   compare it with the hand weights on a held-out quarter. It only becomes active when it is
   clearly better (AUC +0.02) on enough data (MUSE_RANKER_MIN_SAMPLES, default 500).
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np

import taste_models as TM

log = logging.getLogger("muse.ranker")

MIN_SAMPLES = int(os.environ.get("MUSE_RANKER_MIN_SAMPLES", "500"))
HAND = {
    "similarity": 0.28, "facet_fit": 0.14, "audio_sim": 0.14, "behavior_sim": 0.08, "cooccur": 0.08,
    "preference": 0.08, "context_fit": 0.05, "mood_fit": 0.16, "recency_of_similar": 0.05,
    "within_user_popularity": 0.04, "completion_rate": 0.04, "confidence": 0.05, "freshness": 0.03, "goal_fit": 0.08,
}
FEATURES = tuple(HAND)


async def label_outcomes(conn) -> int:
    res = await conn.execute(
        """
        WITH ids AS (
            SELECT i.id, i.shown_at, i.source, i.source_item_id,
                   array_append(COALESCE(array_agg(a.alias_id) FILTER (WHERE a.alias_id IS NOT NULL), '{}'), i.user_id) AS uids
            FROM engine_impression i LEFT JOIN user_alias a ON a.user_id = i.user_id
            WHERE i.outcome IS NULL AND i.shown_at < now() - interval '1 hour'
            GROUP BY i.id
        ), judged AS (
            SELECT ids.id,
                   bool_or(e.action_weight > 0 AND e.action IN ('completed', 'favorited', 'rated', 'added_to_playlist')) AS pos,
                   bool_or(e.action_weight < 0) AS neg,
                   min(ids.shown_at) AS shown_at
            FROM ids LEFT JOIN event e ON e.user_id = ANY(ids.uids) AND e.source = ids.source AND e.source_item_id = ids.source_item_id
                 AND e."timestamp" BETWEEN ids.shown_at AND ids.shown_at + interval '7 days'
            GROUP BY ids.id
        )
        UPDATE engine_impression i SET outcome = CASE WHEN j.pos THEN 'positive' WHEN j.neg THEN 'negative' ELSE 'none' END, outcome_at = now()
        FROM judged j WHERE i.id = j.id AND (j.pos OR j.neg OR j.shown_at < now() - interval '7 days')
        """
    )
    return int(res.split()[-1])


def _matrix(rows) -> tuple[np.ndarray, np.ndarray]:
    x, y = [], []
    for r in rows:
        f = json.loads(r["features"]) if isinstance(r["features"], str) else (r["features"] or {})
        x.append([float(f.get(k, 0.0) or 0.0) for k in FEATURES])
        y.append(1.0 if r["outcome"] == "positive" else 0.0)
    return np.array(x), np.array(y)


async def train(conn) -> list[dict]:
    out = []
    types = [r["item_type"] for r in await conn.fetch(
        "SELECT item_type FROM engine_impression WHERE outcome IS NOT NULL AND shown_at > now() - interval '90 days' GROUP BY 1 HAVING count(*) >= $1",
        MIN_SAMPLES)]
    for item_type in types:
        rows = await conn.fetch(
            "SELECT features, outcome FROM engine_impression WHERE item_type = $1 AND outcome IS NOT NULL AND shown_at > now() - interval '90 days'",
            item_type)
        x, y = _matrix(rows)
        if y.min() == y.max():
            continue
        order = np.random.default_rng(1).permutation(len(y))
        cut = int(len(y) * 0.75)
        tr, te = order[:cut], order[cut:]
        probe = TM.fit_preference(x[tr], y[tr])
        if probe is None or y[te].min() == y[te].max():
            continue
        learned_auc = TM.auc(y[te], np.array([probe.prob(r) for r in x[te]]))
        hand_auc = TM.auc(y[te], x[te] @ np.array([HAND[k] for k in FEATURES]))
        model = TM.fit_preference(x, y)
        raw = model.weights / model.std  # standardised -> raw feature scale (bias is irrelevant for ranking)
        scale = float(np.abs(raw).sum()) or 1.0  # keep scores on a familiar ~0..1 scale
        weights = {k: round(float(v) / scale, 5) for k, v in zip(FEATURES, raw)}
        active = len(y) >= MIN_SAMPLES and learned_auc >= hand_auc + 0.02
        metrics = {"learned_auc": round(learned_auc, 4), "hand_auc": round(hand_auc, 4), "positive_rate": round(float(y.mean()), 4)}
        async with conn.transaction():
            if active:
                await conn.execute("UPDATE engine_ranker_model SET active = FALSE WHERE item_type = $1", item_type)
            await conn.execute(
                "INSERT INTO engine_ranker_model (item_type, weights, metrics, samples, active) VALUES ($1, $2::text::jsonb, $3::text::jsonb, $4, $5)",
                item_type, json.dumps(weights), json.dumps(metrics), len(y), active)
        out.append({"item_type": item_type, "samples": len(y), "active": active, **metrics})
    if out:
        log.info("ranker training: %s", out)
    return out
