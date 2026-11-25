"""Labels logged impressions with what happened next and re-fits the ranker weights
(muse_taste.learn). Runs daily; the recommendation API picks up the latest adopted weights."""

from __future__ import annotations

import logging
import uuid
import asyncpg
import numpy as np
from muse_taste.learn import fit_weights, ips_sample_weight, label_impression
from muse_taste.ranking import DEFAULT_WEIGHTS

from profiles import EVENTS_SQL, to_signal_events

logger = logging.getLogger("muse.worker.ranker")

FEATURES = [f for f in DEFAULT_WEIGHTS if f != "mood"]


async def fit(pool: asyncpg.Pool) -> dict | None:
    async with pool.acquire() as conn:
        imps = await conn.fetch(
            """
            SELECT DISTINCT ON (user_id, canon_key, date_trunc('day', shown_at)) user_id, canon_key, shown_at, features, position
            FROM impression
            WHERE shown_at > now() - interval '120 days' AND shown_at < now() - interval '2 days'
            ORDER BY user_id, canon_key, date_trunc('day', shown_at), shown_at
            """
        )
        if not imps:
            return None
        by_user: dict[str, list] = {}
        for r in imps:
            by_user.setdefault(str(r["user_id"]), []).append(r)
        X, y, w, t = [], [], [], []
        for uid, rows in by_user.items():
            evs = to_signal_events(await conn.fetch(EVENTS_SQL, uuid.UUID(uid), 200))
            by_key: dict[str, list] = {}
            for e in evs:
                by_key.setdefault(e.key, []).append(e)
            for r in rows:
                lw = label_impression(r["shown_at"], by_key.get(r["canon_key"], []))
                feats = r["features"] or {}
                X.append([float(feats.get(f, 0.0)) for f in FEATURES])
                y.append(lw[0])
                # position-bias correction: see muse_taste.learn.ips_sample_weight
                w.append(ips_sample_weight(lw[0], lw[1], int(r["position"] or 0)))
                t.append(r["shown_at"].timestamp())
    if not X:
        return None
    res = fit_weights(FEATURES, np.array(X), np.array(y), np.array(w), np.array(t), DEFAULT_WEIGHTS)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ranker_model (weights, adopted, auc_default, auc_learned, n_train, n_valid, note) VALUES ($1::jsonb, $2, $3, $4, $5, $6, $7)",
            res.weights, res.adopted, res.auc_default, res.auc_learned, res.n_train, res.n_valid, res.note,
        )
    logger.info("ranker fit: %s (auc default %.3f, learned %.3f, n=%d)", res.note, res.auc_default, res.auc_learned, res.n_train + res.n_valid)
    return {"adopted": res.adopted, "note": res.note}
