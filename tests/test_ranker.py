"""Impression outcomes and the learned ranker."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import numpy as np

from conftest import ROOT

sys.path.insert(0, str(ROOT / "services" / "embedding-worker"))
import ranker_training as R  # noqa: E402


async def test_label_and_train(db, monkeypatch):
    conn = await asyncpg.connect(db)
    user, alias = uuid.uuid4(), uuid.uuid4()
    await conn.execute("INSERT INTO user_alias (alias_id, user_id, linked_by) VALUES ($1, $2, 'admin')", alias, user)
    rng = np.random.default_rng(0)
    shown = datetime.now(timezone.utc) - timedelta(days=10)
    req = uuid.uuid4()
    rows, events = [], []
    for i in range(700):
        audio = float(rng.uniform(0, 1))
        feats = {k: float(rng.uniform(0, 1)) for k in R.FEATURES}
        feats["audio_sim"] = audio
        positive = audio + rng.normal(0, 0.15) > 0.75  # sound decides, other features are noise
        rows.append((req, user, "track", "mono", f"t{i}", i % 20, 0.5, False, "taste", "recommendations", json.dumps(feats), shown))
        if positive:  # the alias (e.g. the Mono account) finished it two days later
            events.append((alias, "mono", f"t{i}", "track", "completed", 1.0, shown + timedelta(days=2)))
    await conn.executemany("INSERT INTO impression (request_id, user_id, item_type, source, source_item_id, position, score, explore, generator, "
                           "surface, features, shown_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)", rows)
    await conn.executemany("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, \"timestamp\") "
                           "VALUES ($1,$2,$3,$4,$5,$6,$7)", events)
    assert await R.label_outcomes(conn) == 700
    pos = await conn.fetchval("SELECT count(*) FROM impression WHERE outcome = 'positive'")
    assert pos == len(events)
    out = await R.train(conn)
    assert out[0]["active"] and out[0]["learned_auc"] > out[0]["hand_auc"]
    w = json.loads(await conn.fetchval("SELECT weights FROM ranker_model WHERE active"))
    assert max(w, key=lambda k: abs(w[k])) == "audio_sim" and w["audio_sim"] > 0
    await conn.close()
