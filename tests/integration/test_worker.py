import asyncio
import sys

import asyncpg
from qdrant_client import QdrantClient

from conftest import ROOT, FakeEmbedder, _json_codecs
from fixtures import EXTERNAL_REC, SERIES_TMDB, USER_A, FakeTMDB, seed
from muse_schema.crypto import P_PROFILE, get_vault
from muse_taste.store import ITEMS_COLLECTION, blind_canon, open_canon_data

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


def B(real: str) -> str:
    """The stored (blind) form of a canonical key."""
    return blind_canon(real)


def test_worker_end_to_end(db_dsn):
    async def go():
        pool, qdrant, reports = await run_worker(db_dsn)
        async with pool.acquire() as conn:
            canon = {r["canon_key"]: {**dict(r), "data": open_canon_data(r["data"])} for r in await conn.fetch("SELECT * FROM canon_item")}
            prof = get_vault().open_json(await conn.fetchval("SELECT profile FROM taste_profile WHERE user_id = $1 AND domain = 'video'", USER_A), P_PROFILE)
            lb = await conn.fetchval("SELECT canon_key FROM item WHERE source = 'letterboxd'")
            nf_parent = await conn.fetchval("SELECT p.canon_key FROM item i JOIN item p ON p.item_key = i.parent_key WHERE i.source = 'netflix'")
            pending = await conn.fetchval("SELECT count(*) FROM canon_item WHERE doc_hash IS NOT NULL AND embedded_hash IS DISTINCT FROM doc_hash")
        await pool.close()
        return canon, prof, lb, nf_parent, pending, reports

    canon, prof, lb, nf_parent, pending, reports = asyncio.run(go())
    assert pending == 0
    # identity: the Letterboxd rating and the Netflix episodes land on the library's titles
    assert lb == B("tmdb:movie:104")
    assert nf_parent == B(f"tmdb:tv:{SERIES_TMDB}")
    ext = canon[B(f"tmdb:movie:{EXTERNAL_REC}")]
    assert not ext["available"] and ext["data"]["real_key"] == f"tmdb:movie:{EXTERNAL_REC}"
    c100 = canon[B("tmdb:movie:100")]
    assert c100["available"] and c100["data"]["library_refs"] == [{"source": "jellyfin", "source_item_id": "jf100"}]
    assert "d:crime director" in c100["data"]["tags"]
    assert "Keywords: crime keyword" in c100["data"]["document"]
    # profile (internal keys are blind)
    assert prof["interests"], prof
    assert B("tmdb:movie:104") in prof["seen"] and B(f"tmdb:tv:{SERIES_TMDB}") in prof["seen"]
    anchors = {a["key"]: a for a in prof["anchors"]}
    assert anchors[B(f"tmdb:tv:{SERIES_TMDB}")]["preference"] < 1.5  # 9 episodes = one saturating signal
    assert anchors[B("tmdb:movie:112")]["preference"] < 0  # horror dropped at 9%
    assert any(r["key"] == B(f"tmdb:movie:{EXTERNAL_REC}") for r in prof["related"])
    assert [h["key"] for h in prof["household"]] == [B("tmdb:movie:105")]  # user B overlaps on crime, also liked 105


# Plaintext that must never appear anywhere in the database or the vector store.
SECRETS = ["Crime Film", "Animation Film", "Horror Film", "The Long Case", "detective", "boxd.it", "jf100", "jfseries",
           "tmdb:movie", "tmdb:tv", "Episode 3", "crime keyword", "Crime Director", "Shadow Detective"]


def test_nothing_readable_at_rest(db_dsn):
    """Dump every table and every Qdrant payload as text: no title, id or keyword survives."""
    import json

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        dump = []
        async with pool.acquire() as conn:
            tables = [r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
            for t in tables:
                for r in await conn.fetch(f'SELECT * FROM "{t}"'):
                    dump.append(json.dumps(dict(r), default=str))
        pts, _ = qdrant.scroll(ITEMS_COLLECTION, limit=10000, with_payload=True)
        dump += [json.dumps(p.payload) + str(p.id) for p in pts]
        await pool.close()
        return "\n".join(dump), len(pts)

    text, n_points = asyncio.run(go())
    assert n_points > 0 and len(text) > 1000
    leaks = [s for s in SECRETS if s.lower() in text.lower()]
    assert not leaks, f"plaintext at rest: {leaks}"
