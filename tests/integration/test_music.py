"""Music on the taste engine: Mono as a library source, its signals (unlike, playlist adds),
recommendations Mono can play, next-track and radio."""

import asyncio
import uuid

import asyncpg
from qdrant_client import QdrantClient

from conftest import FakeEmbedder, _json_codecs, use_service
from fixtures import FakeTMDB, insert

MAX = uuid.UUID("33333333-3333-3333-3333-333333333333")
STYLES = {
    "metal": ("Iron Riff", "thrash metal riffs fast aggressive distorted guitars shouting", ["thrash metal"]),
    "ballad": ("Soft Keys", "slow piano ballad sad quiet gentle strings melancholic", ["piano ballad"]),
    "dance": ("Club Beat", "edm club dance drop synth bass festival", ["edm"]),
}


def md(style: str, i: int) -> dict:
    band, words, genres = STYLES[style]
    artist = f"{band} {i % 3}"
    return {"title": f"{artist} - {style.title()} Song {i}", "artist": artist, "album": f"{band} Album",
            "genres": genres, "description": f"Song by {artist}. {words}"}


async def seed(conn):
    for style in STYLES:
        for i in range(10):
            await insert(conn, MAX, "mono", f"{style}{i}", "track", "cataloged", 90, md=md(style, i))
    for i in range(5):  # loves metal and ballads, finished several times
        for d in (30, 20, 10):
            await insert(conn, MAX, "mono", f"metal{i}", "track", "completed", d, 1.0, 100, md("metal", i))
            await insert(conn, MAX, "mono", f"ballad{i}", "track", "completed", d + 1, 1.0, 100, md("ballad", i))
    for i in range(4):  # skips dance
        await insert(conn, MAX, "mono", f"dance{i}", "track", "skipped", 5, -0.5, 10, md("dance", i))
    await insert(conn, MAX, "mono", "metal5", "track", "added_to_playlist", 3, 0.8, md=md("metal", 5))
    await insert(conn, MAX, "mono", "ballad5", "track", "favorited", 3, 1.5, md=md("ballad", 5))
    await insert(conn, MAX, "mono", "ballad5", "track", "unfavorited", 2, -1.5, md=md("ballad", 5))


async def setup(dsn):
    import worker as worker_mod

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, init=_json_codecs)
    async with pool.acquire() as conn:
        await seed(conn)
    qdrant = QdrantClient(":memory:")
    w = worker_mod.Worker(pool, qdrant, FakeEmbedder(), llm=None, tmdb=FakeTMDB())
    await w.prepare()
    for _ in range(3):
        await w.run_once()
    return pool, qdrant


def style(sid: str) -> str:
    return "".join(c for c in sid if c.isalpha())


def test_music_from_mono(db_dsn):
    import sys

    from conftest import ROOT

    sys.path.insert(0, str(ROOT / "services" / "embedding-worker"))
    use_service("recommendation-api")
    from app import main as api
    from app.engine import Engine, Request

    async def go():
        pool, qdrant = await setup(db_dsn)
        eng = Engine(pool, qdrant)
        api._engine = eng
        out = {}
        res = await eng.recommend(Request(str(MAX), "track", limit=6, explore_ratio=0.0, log=False))
        out["recs"] = [eng.describe(s, res.profile, res.titles) for s in res.items]
        out["seen"] = res.profile.seen
        out["next"] = await api.next_up(str(MAX), source="mono", source_item_id="metal1", n=3, history=["mono:metal2"],
                                        discovery=0.3, playable_source=["mono"])
        out["radio"] = await api.radio(str(MAX), seed_source="mono", seed_id="ballad1", length=6, discovery=0.0, playable_source=["mono"])
        out["radio_new"] = await api.radio(str(MAX), seed_source=None, seed_id=None, length=5, discovery=1.0, playable_source=["mono"])
        await pool.close()
        return out

    out = asyncio.run(go())
    recs = out["recs"]
    assert recs and all(r["source"] == "mono" and r["available"] for r in recs), recs
    ids = [r["source_item_id"] for r in recs]
    assert not {f"metal{i}" for i in range(5)} & set(ids)  # heard: not recommended as new
    assert "metal5" not in ids  # in a playlist: known, not new
    assert style(ids[0]) in ("metal", "ballad") and "dance" not in {style(i) for i in ids[:3]}
    assert recs[0]["why"] == recs[0]["reasons"]

    nxt = out["next"]["next"]
    assert nxt and all(n["source"] == "mono" for n in nxt)
    assert style(nxt[0]["source_item_id"]) == "metal" and nxt[0]["source_item_id"] not in ("metal1", "metal2")
    assert nxt[0]["transition"]["flow"] > 0.3

    radio = out["radio"]["tracks"]
    assert len(radio) == 6 and len({t["source_item_id"] for t in radio}) == 6
    assert style(radio[0]["source_item_id"]) == "ballad"  # flows from the ballad seed
    assert any(t["source_item_id"] in {f"ballad{i}" for i in range(5)} for t in radio)  # favourites are radio material
    artists = [t["title"].split(" - ")[0] for t in radio]
    assert all(a != b for a, b in zip(artists, artists[1:]))  # no artist twice in a row

    fresh = out["radio_new"]["tracks"]
    assert fresh and not {f"metal{i}" for i in range(5)} & {t["source_item_id"] for t in fresh}
