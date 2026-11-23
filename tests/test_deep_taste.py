"""Deep taste on a person with two real tastes (fast/aggressive and slow/melancholic) who skips
a third style: facets, sound labels, audio taste, preference model, behaviour, transitions."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import numpy as np
import pytest
from qdrant_client import QdrantClient
from muse_schema.vectors import SealedQdrant
from qdrant_client.http.models import Distance, PointStruct, VectorParams

from muse_testkit import ROOT

sys.path.insert(0, str(ROOT / "services" / "embedding-worker"))
import deep_taste as D  # noqa: E402
import taste_models as TM  # noqa: E402
from muse_schema import content_point_id, taste_point_id  # noqa: E402

PERSON = uuid.UUID("55555555-5555-5555-5555-555555555555")
rng = np.random.default_rng(5)


def around(axis: int, dim: int, n: int, noise=0.15) -> np.ndarray:
    v = rng.normal(0, noise, (n, dim))
    v[:, axis] += 1
    return v / np.linalg.norm(v, axis=1, keepdims=True)


GROUPS = {  # name: (n, text axis, audio axis, features, action)
    "metal": (36, 0, 0, {"energy": 0.9, "danceability": 0.4, "acousticness": 0.1, "brightness": 0.7, "bpm": 180, "mode": "minor"}, "completed"),
    "ballad": (30, 1, 1, {"energy": 0.25, "danceability": 0.3, "acousticness": 0.8, "brightness": 0.3, "bpm": 70, "mode": "minor"}, "completed"),
    "edm": (24, 2, 2, {"energy": 0.85, "danceability": 0.9, "acousticness": 0.05, "brightness": 0.8, "bpm": 128, "mode": "major"}, "skipped"),
}


async def test_deep_taste(db):
    conn = await asyncpg.connect(db)
    qd = SealedQdrant(QdrantClient(":memory:"))  # payloads sealed at rest, as in production
    qd.create_collection("content_items", vectors_config=VectorParams(size=384, distance=Distance.COSINE))
    qd.create_collection("audio_items", vectors_config=VectorParams(size=512, distance=Distance.COSINE))
    D.ensure_collections(qd)
    t0 = datetime.now(timezone.utc) - timedelta(days=20)
    titles = {}
    for name, (n, tax, aax, feats, action) in GROUPS.items():
        tv, av = around(tax, 384, n), around(aax, 512, n)
        for i in range(n):
            sid = f"{name}{i}"
            title = titles[sid] = f"{name.title()} Band {i % 3} - Song {i}"
            pid = content_point_id("mono", sid)
            qd.upsert("content_items", [PointStruct(id=pid, vector=tv[i].tolist(), payload={"source": "mono", "source_item_id": sid, "title": title, "genres": [name]})])
            qd.upsert("audio_items", [PointStruct(id=pid, vector=av[i].tolist(), payload={"source": "mono", "source_item_id": sid, "title": title})])
            f = {k: (v + rng.normal(0, 0.05) if isinstance(v, float) else v) for k, v in feats.items()}
            await conn.execute("INSERT INTO item_audio (source, source_item_id, status, features) VALUES ('mono', $1, 'done', $2::jsonb)", sid, json.dumps(f))
    # Three rounds of listening sessions; each session stays within one style.
    ts = t0
    for rnd in range(3):
        for name, (n, _, _, _, action) in GROUPS.items():
            order = rng.permutation(n)
            for chunk in range(0, n, 10):
                ts += timedelta(hours=2)  # new session
                for i in order[chunk:chunk + 10]:
                    ts += timedelta(minutes=4)
                    sid = f"{name}{i}"
                    await conn.execute(
                        "INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, \"timestamp\", metadata) "
                        "VALUES ($1, 'mono', $2, 'track', $3, $4, $5, $6::jsonb)",
                        PERSON, sid, action, 1.0 if action == "completed" else -0.5, ts, json.dumps({"title": titles[sid]}))

    desc = rng.normal(0, 1, (len(D.DESCRIPTORS), 512))
    desc[D.DESCRIPTORS.index("aggressive")] = np.eye(512)[0] * 5
    desc[D.DESCRIPTORS.index("melancholic")] = np.eye(512)[1] * 5
    desc /= np.linalg.norm(desc, axis=1, keepdims=True)

    pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
    out = await D.run(pool, qd, [str(PERSON)], desc)
    await pool.close()

    info = out[str(PERSON)]["track"]
    assert info["text_facets"] == 2 and info["audio_facets"] == 2  # two tastes, not one average
    facets = await conn.fetch("SELECT * FROM taste_facet WHERE user_id = $1 AND space = 'audio' ORDER BY idx", PERSON)
    labels = [f["label"] for f in facets]
    assert labels[0].startswith("aggressive") and labels[1].startswith("melancholic")
    assert json.loads(facets[0]["features"])["bpm"] > 150 > json.loads(facets[1]["features"])["bpm"]
    text_labels = [f["label"] for f in await conn.fetch("SELECT label FROM taste_facet WHERE user_id = $1 AND space = 'text' ORDER BY idx", PERSON)]
    assert text_labels[0].startswith("metal") and text_labels[1].startswith("ballad")

    # Audio taste points away from what's skipped.
    at = np.array(qd.retrieve("audio_taste", [taste_point_id(str(PERSON), "track")], with_vectors=True)[0].vector)
    assert at[0] > 0 and at[1] > 0 and at[2] < 0

    model = await conn.fetchrow("SELECT * FROM taste_model WHERE user_id = $1", PERSON)
    assert json.loads(model["metrics"])["auc"] >= 0.6
    summary = json.loads(model["model"])["summary"]
    assert summary and all(s.startswith("prefers") for s in summary)
    pm = TM.PreferenceModel.from_json(json.loads(model["model"]))
    assert pm.prob(TM.feature_vector(GROUPS["ballad"][3])) > pm.prob(TM.feature_vector(GROUPS["edm"][3]))

    hh = out["household"]
    assert hh["behavior_items"] > 20 and hh["transitions"] > 20
    tr = await conn.fetchrow("SELECT * FROM item_transition ORDER BY count + completions DESC LIMIT 1")
    assert tr["from_key"].split(":")[1][:4] == tr["to_key"].split(":")[1][:4]  # metal follows metal
    assert qd.retrieve("behavior_taste", [taste_point_id(str(PERSON), "track")])
    await conn.close()
