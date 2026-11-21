import asyncio
import sys
from pathlib import Path

import asyncpg
import pytest
from qdrant_client import QdrantClient

from conftest import ROOT, FakeEmbedder, _json_codecs
from fixtures import EXTERNAL_REC, SERIES_TMDB, USER_A, FakeTMDB, seed

sys.path.insert(0, str(ROOT / "services" / "embedding-worker"))


async def run_worker(dsn, cycles=3):
    import worker as worker_mod

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, init=_json_codecs)
    async with pool.acquire() as conn:
        await seed(conn)
    qdrant = QdrantClient(":memory:")
    tmdb = FakeTMDB()
    w = worker_mod.Worker(pool, qdrant, FakeEmbedder(), llm=None, tmdb=tmdb)
    await w.prepare()
    reports = [await w.run_once() for _ in range(cycles)]
    return pool, qdrant, reports


def test_worker_end_to_end(db_dsn):
    async def go():
        pool, qdrant, reports = await run_worker(db_dsn)
        async with pool.acquire() as conn:
            canon = {r["canon_key"]: r for r in await conn.fetch("SELECT * FROM canon_item")}
            prof = await conn.fetchval("SELECT profile FROM taste_profile WHERE user_id = $1 AND domain = 'video'", USER_A)
            lb = await conn.fetchval("SELECT canon_key FROM item WHERE source = 'letterboxd'")
            nf_parent = await conn.fetchval("SELECT p.canon_key FROM item i JOIN item p ON p.item_key = i.parent_key WHERE i.source = 'netflix'")
            pending = await conn.fetchval("SELECT count(*) FROM canon_item WHERE embedded_hash IS DISTINCT FROM doc_hash")
        await pool.close()
        return canon, prof, lb, nf_parent, pending, reports

    canon, prof, lb, nf_parent, pending, reports = asyncio.run(go())
    assert pending == 0
    # identity: the Letterboxd rating and the Netflix episodes land on the library's titles
    assert lb == "tmdb:movie:104"
    assert nf_parent == f"tmdb:tv:{SERIES_TMDB}"
    assert f"tmdb:movie:{EXTERNAL_REC}" in canon and not canon[f"tmdb:movie:{EXTERNAL_REC}"]["available"]
    assert canon["tmdb:movie:100"]["available"] and canon["tmdb:movie:100"]["library_refs"] == [{"source": "jellyfin", "source_item_id": "jf100"}]
    assert "d:crime director" in canon["tmdb:movie:100"]["tags"]
    assert "Keywords: crime keyword" in canon["tmdb:movie:100"]["document"]
    # profile
    assert prof["interests"], prof
    assert "tmdb:movie:104" in prof["seen"] and f"tmdb:tv:{SERIES_TMDB}" in prof["seen"]
    anchors = {a["key"]: a for a in prof["anchors"]}
    assert anchors[f"tmdb:tv:{SERIES_TMDB}"]["preference"] < 1.5  # 9 episodes = one saturating signal
    assert anchors["tmdb:movie:112"]["preference"] < 0  # horror dropped at 9%
    assert any(r["key"] == f"tmdb:movie:{EXTERNAL_REC}" for r in prof["related"])
    assert [h["key"] for h in prof["household"]] == ["tmdb:movie:105"]  # user B overlaps on crime, also liked 105
