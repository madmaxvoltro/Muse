import asyncio
import sys
import uuid

import pytest

from conftest import use_service
from fixtures import EXTERNAL_REC, USER_A, insert
from test_worker import run_worker



def test_recommendations_questions_and_feedback(db_dsn):
    use_service("recommendation-api")
    from app import questions
    from app.engine import Engine, Request

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        eng = Engine(pool, qdrant)
        uid = str(USER_A)
        out = {}

        res = await eng.recommend(Request(uid, "movie", limit=8, explore_ratio=0.0))
        out["lib"] = [eng.describe(s, res.profile, res.titles) for s in res.items]
        res2 = await eng.recommend(Request(uid, "movie", limit=20, explore_ratio=0.0, include_unavailable=True, log=False))
        out["all"] = [eng.describe(s, res2.profile, res2.titles) for s in res2.items]
        async with pool.acquire() as conn:
            out["impressions"] = await conn.fetchval("SELECT count(*) FROM impression WHERE user_id = $1", USER_A)
            # immediate feedback: thumbs down on the current top pick, no worker cycle in between
            top = out["lib"][0]
            await insert(conn, USER_A, "jellyfin", top["source_item_id"], "movie", "rated", 0, -1.0, md={"value": -1})
        res3 = await eng.recommend(Request(uid, "movie", limit=8, explore_ratio=0.0, log=False))
        out["after"] = [s.key for s in res3.items]
        out["neg_keys"] = [n.key for n in res3.profile.negatives]

        out["onboarding"] = await questions.onboarding(eng, str(uuid.uuid4()), "movie", n=6)
        out["abandoned"] = await questions.abandoned(eng, uid, "movie")
        out["pair"] = await questions.pairwise(eng, uid, "movie")
        cold = await eng.recommend(Request(str(uuid.uuid4()), "movie", limit=5, log=False))
        out["cold"] = (cold.cold_start, [s.key for s in cold.items])
        await pool.close()
        return out

    out = asyncio.run(go())
    lib = out["lib"]
    seen = {"tmdb:movie:100", "tmdb:movie:101", "tmdb:movie:102", "tmdb:movie:103", "tmdb:movie:104", "tmdb:movie:106", "tmdb:movie:112"}
    keys = [r["canon_key"] for r in lib]
    assert not seen & set(keys), keys
    assert all(r["available"] and r["source"] == "jellyfin" for r in lib)
    assert keys[0] == "tmdb:movie:105", lib[:3]  # the one unseen crime film, also liked by user B
    assert lib[0]["reasons"] and isinstance(lib[0]["explanation"], dict)
    assert all(isinstance(v, float) for v in lib[0]["explanation"].values())
    horror = [k for k in keys if k in {f"tmdb:movie:{i}" for i in range(112, 118)}]
    assert not horror or keys.index(horror[0]) > keys.index("tmdb:movie:105")
    ext = [r for r in out["all"] if r["canon_key"] == f"tmdb:movie:{EXTERNAL_REC}"]
    assert ext and not ext[0]["available"] and ext[0]["source"] == "tmdb"
    assert any("also liked" in reason for reason in ext[0]["reasons"]), ext[0]
    assert out["impressions"] == len(lib)
    assert "tmdb:movie:105" not in out["after"] and "tmdb:movie:105" in out["neg_keys"]
    assert len(out["onboarding"]["items"]) == 6 and out["onboarding"]["question_id"]
    assert [i["canon_key"] for i in out["abandoned"]["items"]] == ["tmdb:movie:112"]
    assert out["pair"]["question_id"] and out["pair"]["a"]["canon_key"] != out["pair"]["b"]["canon_key"]
    assert out["cold"][0] and out["cold"][1]


def test_http_routes(db_dsn):
    use_service("recommendation-api")
    import httpx
    from app import main
    from app.engine import Engine

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        main._engine = Engine(pool, qdrant)
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/recommendations/{USER_A}", params={"item_type": "movie", "limit": 5})
            p = await c.get(f"/profile/{USER_A}")
            q = await c.get(f"/questions/{USER_A}/next", params={"item_type": "movie"})
            bad = await c.get(f"/recommendations/{USER_A}", params={"item_type": "nope"})
        main._engine = None
        await pool.close()
        return r, p, q, bad

    r, p, q, bad = asyncio.run(go())
    assert r.status_code == 200 and r.json()["recommendations"][0]["canon_key"] == "tmdb:movie:105"
    assert {"source", "source_item_id", "score", "confidence", "explanation"} <= set(r.json()["recommendations"][0])  # plugin contract
    assert p.status_code == 200 and p.json()["interests"] and p.json()["stats"]["liked"] > 0
    assert q.status_code == 200 and q.json()["kind"] in ("onboarding", "abandon", "this_or_that")
    assert bad.status_code == 400
