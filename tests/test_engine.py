"""The recommendation engine end to end on a small household:
A loves fast metal and slow ballads and skips EDM; B loves EDM and ballads."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import numpy as np
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams

from conftest import ROOT, FakeModel
from test_flow import services

sys.path.insert(0, str(ROOT / "services" / "embedding-worker"))
import deep_taste as D  # noqa: E402
from muse_schema import content_point_id  # noqa: E402

A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")
rng = np.random.default_rng(11)
STYLES = {
    "metal": dict(n=22, axis=0, feats={"bpm": 172.0, "camelot": "8A", "mode": "minor", "energy": 0.92, "danceability": 0.4, "acousticness": 0.1,
                                        "brightness": 0.7, "percussiveness": 0.6}, words="thrash metal riffs fast aggressive"),
    "ballad": dict(n=18, axis=1, feats={"bpm": 68.0, "camelot": "9A", "mode": "minor", "energy": 0.18, "danceability": 0.25, "acousticness": 0.85,
                                         "brightness": 0.3, "percussiveness": 0.15}, words="slow piano ballad sad quiet"),
    "edm": dict(n=14, axis=2, feats={"bpm": 128.0, "camelot": "3B", "mode": "major", "energy": 0.85, "danceability": 0.92, "acousticness": 0.05,
                                      "brightness": 0.85, "percussiveness": 0.7}, words="edm club dance drop synth"),
}


def vec(axis: int, dim: int) -> list[float]:
    v = rng.normal(0, 0.12, dim)
    v[axis] += 1
    return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
async def eng(db):
    worker, rec, query = services()
    qd = QdrantClient(":memory:")
    worker.ensure_collections(qd)
    qd.create_collection("audio_items", vectors_config=VectorParams(size=512, distance=Distance.COSINE))
    D.ensure_collections(qd)
    rec._qdrant = qd
    sys.modules["recapi.db"]._pool = None
    conn = await asyncpg.connect(db)
    t = datetime.now(timezone.utc) - timedelta(days=10)
    for style, cfg in STYLES.items():
        for i in range(cfg["n"]):
            sid = f"{style}{i}"
            title = f"{style.title()} Band {i % 4} - {cfg['words'].split()[0].title()} {i}"
            md = {"title": title, "artist": f"{style.title()} Band {i % 4}", "genres": [style], "description": cfg["words"]}
            await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, metadata, \"timestamp\") "
                               "VALUES ($1, 'mono', $2, 'track', 'cataloged', $3::jsonb, $4)", A, sid, json.dumps(md), t - timedelta(days=30))
            f = {k: (v + rng.normal(0, 0.03) if isinstance(v, float) else v) for k, v in cfg["feats"].items()}
            await conn.execute("INSERT INTO item_audio (source, source_item_id, status, features) VALUES ('mono', $1, 'done', $2::jsonb)", sid, json.dumps(f))
            qd.upsert("audio_items", [PointStruct(id=content_point_id("mono", sid), vector=vec(cfg["axis"], 512),
                                                 payload={"source": "mono", "source_item_id": sid, "item_type": "track", "title": title, **f})])

    async def listen(user, style, idxs, action, weight, rounds=3):
        nonlocal t
        for _ in range(rounds):
            t += timedelta(hours=3)
            for i in rng.permutation(idxs):
                t += timedelta(minutes=4)
                await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, \"timestamp\", metadata) "
                                   "VALUES ($1, 'mono', $2, 'track', $3, $4, $5, '{}'::jsonb)", user, f"{style}{i}", action, weight, t)

    await listen(A, "metal", range(12), "completed", 1.0)
    await listen(A, "ballad", range(10), "completed", 1.0)
    await listen(A, "edm", range(6), "skipped", -0.5, rounds=1)
    await listen(B, "edm", range(10), "completed", 1.0)
    await listen(B, "ballad", range(5, 14), "completed", 1.0)
    await listen(B, "metal", range(4), "skipped", -0.5, rounds=1)
    await conn.close()

    pool = await sys.modules["queryapi.db"].get_pool()
    await worker.run_once(pool, qd, FakeModel())
    await D.run(pool, qd, [str(A), str(B)], None)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=rec.app), base_url="http://rec")
    yield {"c": client, "db": db, "qd": qd}
    await client.aclose()
    await sys.modules["queryapi.db"].close_pool()
    await sys.modules["recapi.db"].close_pool()


def style(r):
    return "".join(ch for ch in r["source_item_id"] if ch.isalpha())


async def test_recommendations_cover_both_tastes(eng):
    r = (await eng["c"].get(f"/recommendations/{A}", params={"item_type": "track", "limit": 8, "source": "mono"})).json()
    styles = [style(x) for x in r["recommendations"]]
    assert "metal" in styles[:6] and "ballad" in styles[:6]  # two tastes, both served
    top_edm = [s for s in styles[:5] if s == "edm"]
    assert not top_edm
    first = r["recommendations"][0]
    assert {"audio_sim", "facet_fit", "similarity"} <= set(first["explanation"]) and first["why"]
    assert first["facet"] and first["audio"]["bpm"]
    conn = await asyncpg.connect(eng["db"])
    assert await conn.fetchval("SELECT count(*) FROM impression WHERE request_id = $1", uuid.UUID(r["request_id"])) == len(styles)
    await conn.close()


async def test_mood_changes_the_list(eng):
    sleep = (await eng["c"].get(f"/recommendations/{A}", params={"item_type": "track", "limit": 5, "mood": "sleep", "log": False})).json()
    assert [style(x) for x in sleep["recommendations"][:3]] == ["ballad"] * 3
    assert sleep["context"]["mood"] == "sleep" and any("sleep" in w for w in sleep["recommendations"][0]["why"])
    work = (await eng["c"].get(f"/recommendations/{A}", params={"item_type": "track", "limit": 5, "mood": "workout", "log": False})).json()
    assert style(work["recommendations"][0]) == "metal"


async def test_next_track_flows(eng):
    r = (await eng["c"].get(f"/next/{A}", params={"source": "mono", "source_item_id": "metal3", "n": 3})).json()
    nxt = r["next"]
    assert style(nxt[0]) == "metal" and nxt[0]["transition"]["key"] >= 0.9 and nxt[0]["transition"]["tempo"] >= 0.9
    assert nxt[0]["source_item_id"] != "metal3" and r["after"]["audio"]["bpm"] > 150


async def test_radio_follows_energy_arc(eng):
    r = (await eng["c"].get(f"/radio/{A}", params={"seed_source": "mono", "seed_id": "metal2", "length": 10, "arc": "wind_down",
                                                     "discovery": 0.0})).json()
    styles = [style(x) for x in r["tracks"]]
    assert len(styles) == 10 and len({x["source_item_id"] for x in r["tracks"]}) == 10
    assert styles[0] == "metal" and styles[-1] == "ballad"  # from loud to calm
    assert "edm" not in styles
    steady = (await eng["c"].get(f"/radio/{A}", params={"seed_source": "mono", "seed_id": "metal2", "length": 6})).json()
    assert [style(x) for x in steady["tracks"]].count("metal") >= 5


async def test_group_is_fair_and_vetoes(eng):
    r = (await eng["c"].get("/group-recommendations", params={"user_id": [str(A), str(B)], "item_type": "track", "limit": 5})).json()
    recs = r["recommendations"]
    assert style(recs[0]) == "ballad"  # the overlap wins
    assert set(recs[0]["member_fit"]) == {str(A), str(B)}
    ids = {x["source_item_id"] for x in recs}
    assert not ids & {f"edm{i}" for i in range(6)}  # A skipped these: vetoed
    assert not ids & {f"metal{i}" for i in range(4)}  # B skipped these
    alone = (await eng["c"].get("/group-recommendations", params={"user_id": [str(B)], "item_type": "track", "limit": 3})).json()
    assert style(alone["recommendations"][0]) in ("edm", "ballad")
    fit = (await eng["c"].get("/fit/mono/ballad16", params={"user_id": [str(A), str(B)], "item_type": "track"})).json()["fits"]
    assert fit[str(A)] > 0.4 and fit[str(B)] > 0.4


async def test_context_taste_vectors(db):
    """Night listening differs from day listening: the worker learns a 'track@night' taste."""
    worker, rec, query = services()
    qd = QdrantClient(":memory:")
    worker.ensure_collections(qd)
    conn = await asyncpg.connect(db)
    for i in range(20):
        for style_, hour in (("ballad", 23), ("metal", 14)):
            md = {"title": f"{style_} song {i}", "genres": [style_], "description": STYLES[style_]["words"]}
            await conn.execute(
                "INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, metadata, device_context) "
                "VALUES ($1, 'mono', $2, 'track', 'completed', 1.0, $3::jsonb, $4::jsonb)",
                A, f"{style_}{i}", json.dumps(md), json.dumps({"device": "Pixel", "local_hour": hour, "weekday": 2}))
    await conn.close()
    pool = await sys.modules["queryapi.db"].get_pool()
    await worker.run_once(pool, qd, FakeModel())
    await sys.modules["queryapi.db"].close_pool()
    from muse_schema import taste_point_id

    night = qd.retrieve("taste_vectors", [taste_point_id(str(A), "track@night")], with_vectors=True)
    day = qd.retrieve("taste_vectors", [taste_point_id(str(A), "track@afternoon")], with_vectors=True)
    phone = qd.retrieve("taste_vectors", [taste_point_id(str(A), "track@phone")], with_vectors=True)
    assert night and day and phone
    ballad_vec = FakeModel().encode("ballad song 1 ballad slow piano ballad sad quiet")
    assert float(np.dot(night[0].vector, ballad_vec)) > float(np.dot(day[0].vector, ballad_vec))
