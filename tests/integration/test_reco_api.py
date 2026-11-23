import asyncio
import uuid

from conftest import use_service
from fixtures import EXTERNAL_REC, USER_A, USER_B, insert
from test_worker import B, run_worker
from muse_schema.crypto import P_PROFILE, P_QUESTION, get_vault



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
    seen = {B(f"tmdb:movie:{i}") for i in (100, 101, 102, 103, 104, 106, 112)}
    keys = [r["canon_key"] for r in lib]
    assert not seen & set(keys), keys
    assert all(r["available"] and r["source"] == "jellyfin" for r in lib)
    assert keys[0] == B("tmdb:movie:105"), lib[:3]  # the one unseen crime film, also liked by user B
    assert lib[0]["tmdb_id"] == 105 and lib[0]["title"] == "Crime Film 5"
    assert lib[0]["reasons"] and isinstance(lib[0]["explanation"], dict)
    assert all(isinstance(v, float) for v in lib[0]["explanation"].values())
    horror = [k for k in keys if k in {B(f"tmdb:movie:{i}") for i in range(112, 118)}]
    assert not horror or keys.index(horror[0]) > keys.index(B("tmdb:movie:105"))
    ext = [r for r in out["all"] if r["canon_key"] == B(f"tmdb:movie:{EXTERNAL_REC}")]
    assert ext and not ext[0]["available"] and ext[0]["source"] == "tmdb"
    assert any("also liked" in reason for reason in ext[0]["reasons"]), ext[0]
    assert out["impressions"] == len(lib)
    assert B("tmdb:movie:105") not in out["after"] and B("tmdb:movie:105") in out["neg_keys"]
    assert len(out["onboarding"]["items"]) == 6 and out["onboarding"]["question_id"]
    assert [i["canon_key"] for i in out["abandoned"]["items"]] == [B("tmdb:movie:112")]
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
    assert r.status_code == 200 and r.json()["recommendations"][0]["canon_key"] == B("tmdb:movie:105")
    assert {"source", "source_item_id", "score", "confidence", "explanation"} <= set(r.json()["recommendations"][0])  # plugin contract
    assert p.status_code == 200 and p.json()["interests"] and p.json()["stats"]["liked"] > 0
    assert q.status_code == 200 and q.json()["kind"] in ("onboarding", "abandon", "this_or_that")
    assert bad.status_code == 400


def test_evolution_endpoint_and_strength(db_dsn):
    use_service("recommendation-api")
    import httpx
    from app import main
    from app.engine import Engine, Request

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        eng = Engine(pool, qdrant)
        main._engine = eng
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            evo = await c.get(f"/evolution/{USER_A}")
        on = await eng.recommend(Request(str(USER_A), "movie", limit=10, explore_ratio=0.2, include_unavailable=True, log=False))
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO user_overlay (user_id, evolution_strength) VALUES ($1, 0)", USER_A)
        off = await eng.recommend(Request(str(USER_A), "movie", limit=10, explore_ratio=0.2, include_unavailable=True, log=False))
        main._engine = None
        await pool.close()
        return evo, on, off

    evo, on, off = asyncio.run(go())
    assert evo.status_code == 200, evo.text
    body = evo.json()
    assert body["interests"] and all(i["state"] in ("emerging", "growing", "stable", "fading", "dormant", "pinned") for i in body["interests"])
    assert body["timeline"] and body["stretch"]["band"] > 0
    feats_on = [s.features for s in on.pool]
    feats_off = [s.features for s in off.pool]
    assert any(abs(f["trend"]) > 0 for f in feats_on)
    assert all(f["trend"] == 0 and f["stretch"] == 0 and f["context"] == 0 for f in feats_off)


def test_question_queue_end_to_end(db_dsn, monkeypatch):
    """Worker fills the queue; API serves it; answers are optional and steer evolution."""
    import httpx

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        async with pool.acquire() as conn:
            queued = await conn.fetch("SELECT kind, dedupe_key, status, payload FROM question_log WHERE user_id = $1", USER_A)
            prof = get_vault().open_json(await conn.fetchval("SELECT profile FROM taste_profile WHERE user_id = $1 AND domain = 'video'", USER_A), P_PROFILE)
            it = prof["interests"][0]
            # an interest question as the producer would write it
            qid = uuid.uuid4()
            await conn.execute(
                """INSERT INTO question_log (id, user_id, kind, payload, status, domain, priority, dedupe_key, expires_at)
                   VALUES ($1, $2, 'interest', $3::jsonb, 'pending', 'video', 0.6, 'interest:emerging:test', now() + interval '1 day')""",
                qid, USER_A, get_vault().seal_json({"subkind": "emerging", "prompt": "More?", "interest": {"anchor_key": it["medoid_key"], "members": it["members"]}}, P_QUESTION),
            )
            stale = uuid.uuid4()
            await conn.execute("INSERT INTO question_log (id, user_id, kind, payload, status, expires_at) VALUES ($1, $2, 'abandon', '{}'::jsonb, 'pending', now() - interval '1 hour')", stale, USER_A)

        use_service("recommendation-api")
        from app import main as reco_main
        from app.engine import Engine

        reco_main._engine = Engine(pool, qdrant)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reco_main.app), base_url="http://t") as c:
            q1 = (await c.get(f"/questions/{USER_A}/queue", params={"limit": 20})).json()
            q2 = (await c.get(f"/questions/{USER_A}/queue", params={"limit": 20, "include_served": "false"})).json()
        reco_main._engine = None

        monkeypatch.setenv("MUSE_DATABASE_URL", db_dsn)
        monkeypatch.setenv("MUSE_ADAPTER_KEYS", "muse:mk")
        use_service("ingestion-api")
        import importlib

        import app.auth
        import app.db
        import app.main as ing_main

        importlib.reload(app.auth)
        importlib.reload(app.db)
        ing_main = importlib.reload(ing_main)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ing_main.app), base_url="http://t") as c:
            ans = await c.post("/answers", headers={"X-Muse-Api-Key": "mk"}, json={"kind": "interest", "user_id": str(USER_A), "question_id": str(qid), "response": "more"})
            other = await c.post("/answers", headers={"X-Muse-Api-Key": "mk"}, json={"kind": "interest", "user_id": str(USER_B), "question_id": str(qid), "response": "less"})
        await ing_main.close_pool()

        import worker as worker_mod
        from conftest import FakeEmbedder
        from fixtures import FakeTMDB

        w = worker_mod.Worker(pool, qdrant, FakeEmbedder(), tmdb=FakeTMDB())
        await w.run_once()
        async with pool.acquire() as conn:
            statuses = {r["id"]: r["status"] for r in await conn.fetch("SELECT id, status FROM question_log WHERE id = ANY($1::uuid[])", [qid, stale])}
            prof2 = get_vault().open_json(await conn.fetchval("SELECT profile FROM taste_profile WHERE user_id = $1 AND domain = 'video'", USER_A), P_PROFILE)
            dupes = await conn.fetchval("SELECT count(*) - count(DISTINCT dedupe_key) FROM question_log WHERE user_id = $1 AND dedupe_key IS NOT NULL", USER_A)
        await pool.close()
        return queued, q1, q2, ans, other, statuses, prof2, qid, stale, dupes

    queued, q1, q2, ans, other, statuses, prof2, qid, stale, dupes = asyncio.run(go())
    kinds = {r["kind"] for r in queued}
    assert "abandon" in kinds and "this_or_that" in kinds, queued
    assert any(r["dedupe_key"] == get_vault().bid("abandon:" + B("tmdb:movie:112"), P_QUESTION) for r in queued)
    assert all("_enc" in r["payload"] for r in queued)  # question payloads are sealed
    ids = [q["question_id"] for q in q1["questions"]]
    assert str(qid) in ids and str(stale) not in ids  # expired questions are never served
    assert all("answer_with" in q for q in q1["questions"])
    assert q2["questions"] == []  # everything was marked served by the first fetch
    assert ans.status_code == 202 and ans.json()["events"] == 1
    assert other.status_code == 403
    assert statuses[qid] == "answered" and statuses[stale] == "expired"
    assert any(v.get("steer", 0) > 0.5 for v in prof2["evolution"]["interests"].values())
    assert dupes == 0  # a second worker cycle doesn't re-queue the same questions


def test_status_page_and_secure_defaults(db_dsn, monkeypatch):
    use_service("recommendation-api")
    import httpx
    from app import main
    from app.engine import Engine

    monkeypatch.setenv("MUSE_ALLOW_OPEN_READ", "false")
    monkeypatch.setenv("MUSE_STATUS_TOKEN", "status-secret")
    monkeypatch.setattr(main, "_READ_KEYS", {"read-secret": "vesper"})

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        main._engine = Engine(pool, qdrant)
        monkeypatch.setattr(main, "get_pool", lambda: _ret(pool))
        out = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
            out["no_key"] = (await c.get(f"/profile/{USER_A}")).status_code
            out["bad_key"] = (await c.get(f"/profile/{USER_A}", headers={"X-Muse-Read-Key": "nope"})).status_code
            out["good_key"] = (await c.get(f"/profile/{USER_A}", headers={"X-Muse-Read-Key": "read-secret"})).status_code
            out["status_no_token"] = (await c.get("/status")).status_code
            r = await c.get("/status?token=status-secret")
            out["redirect"] = (r.status_code, r.headers.get("location"), r.headers.get("set-cookie", ""))
            page = await c.get("/status", cookies={"muse_status": "status-secret"})
            out["page"] = (page.status_code, page.text)
            out["json"] = (await c.get("/status.json", cookies={"muse_status": "status-secret"})).json()
            out["health"] = (await c.get("/healthz")).status_code
        main._engine = None
        await pool.close()
        return out

    async def _ret(v):
        return v

    out = asyncio.run(go())
    assert out["no_key"] == 401 and out["bad_key"] == 401 and out["good_key"] == 200 and out["health"] == 200
    assert out["status_no_token"] == 401
    code, loc, cookie = out["redirect"]
    assert code == 303 and "token" not in loc and "HttpOnly" in cookie
    status_code, text = out["page"]
    assert status_code == 200 and "Muse" in text
    assert not any(s in text for s in ("Crime Film", "jf100", "tmdb:", "status-secret"))  # counts only
    assert out["json"]["catalog"]["titles"] > 0 and out["json"]["profiles"] >= 1


def test_fresh_install_without_catalog_is_graceful(db_dsn):
    """Before the worker's first cycle there is no vector collection: answer, don't 500."""
    use_service("recommendation-api")
    import asyncpg
    import httpx
    from app import main
    from app.engine import Engine
    from qdrant_client import QdrantClient

    from conftest import _json_codecs

    async def go():
        pool = await asyncpg.create_pool(db_dsn, min_size=1, max_size=2, init=_json_codecs)
        main._engine = Engine(pool, QdrantClient(":memory:"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
            r = await c.get(f"/recommendations/{USER_A}", params={"item_type": "movie"})
            q = await c.get(f"/questions/{USER_A}/next")
        main._engine = None
        await pool.close()
        return r, q

    r, q = asyncio.run(go())
    assert r.status_code == 200 and r.json()["recommendations"] == [] and "catalog" in r.json()["note"]
    assert q.status_code == 200 and q.json()["question_id"] is None


def test_scrutable_reasons_feedback_loop(db_dsn, monkeypatch):
    """A reason can be corrected; the correction applies on the next request and sticks."""
    import importlib

    import httpx

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        use_service("recommendation-api")
        from app.engine import Engine, Request

        eng = Engine(pool, qdrant)
        res = await eng.recommend(Request(str(USER_A), "movie", limit=10, explore_ratio=0.0, include_unavailable=True, log=False))
        described = [eng.describe(s, res.profile, res.titles) for s in res.items]
        anchor_reasons = [d for r in described for d in r["reason_details"] if d["type"] == "anchor"]
        assert anchor_reasons, described
        target = anchor_reasons[0]

        monkeypatch.setenv("MUSE_DATABASE_URL", db_dsn)
        monkeypatch.setenv("MUSE_ADAPTER_KEYS", "muse:mk")
        use_service("ingestion-api")
        import app.auth
        import app.db
        import app.main as ing

        importlib.reload(app.auth)
        importlib.reload(app.db)
        ing = importlib.reload(ing)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ing.app), base_url="http://t") as c:
            r = await c.post("/answers", headers={"X-Muse-Api-Key": "mk"},
                             json={"kind": "reason", "user_id": str(USER_A), "reason_id": target["id"], "response": "not_relevant"})
            bad = await c.post("/answers", headers={"X-Muse-Api-Key": "mk"},
                               json={"kind": "reason", "user_id": str(USER_A), "reason_id": "quality", "response": "less"})
        await ing.close_pool()

        res2 = await eng.recommend(Request(str(USER_A), "movie", limit=10, explore_ratio=0.0, include_unavailable=True, log=False))
        after = [d["id"] for s in res2.items for d in eng.describe(s, res2.profile, res2.titles)["reason_details"]]

        import worker as worker_mod
        from conftest import FakeEmbedder
        from fixtures import FakeTMDB

        await worker_mod.Worker(pool, qdrant, FakeEmbedder(), tmdb=FakeTMDB()).run_once()
        async with pool.acquire() as conn:
            prof = get_vault().open_json(await conn.fetchval("SELECT profile FROM taste_profile WHERE user_id = $1 AND domain = 'video'", USER_A), P_PROFILE)
        await pool.close()
        return target, r, bad, after, prof

    target, r, bad, after, prof = asyncio.run(go())
    anchor_key = target["id"].split(":", 1)[1]
    assert target["responses"] == ["not_relevant", "more"]
    assert r.status_code == 202 and r.json()["events"] == 1
    assert bad.status_code == 422
    assert target["id"] not in after  # immediate, before any worker cycle
    assert anchor_key in prof["muted"] and anchor_key not in [a["key"] for a in prof["anchors"]]
    assert prof["reconsume"] is not None


def test_tag_question_answer_changes_profile(db_dsn, monkeypatch):
    import importlib

    import httpx

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, payload FROM question_log WHERE user_id = $1 AND kind = 'tag'", USER_A)
        assert rows, "no tag question queued"
        qid, payload = rows[0]["id"], get_vault().open_json(rows[0]["payload"], P_QUESTION)

        monkeypatch.setenv("MUSE_DATABASE_URL", db_dsn)
        monkeypatch.setenv("MUSE_ADAPTER_KEYS", "muse:mk")
        use_service("ingestion-api")
        import app.auth
        import app.db
        import app.main as ing

        importlib.reload(app.auth)
        importlib.reload(app.db)
        ing = importlib.reload(ing)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ing.app), base_url="http://t") as c:
            r = await c.post("/answers", headers={"X-Muse-Api-Key": "mk"},
                             json={"kind": "tag", "user_id": str(USER_A), "question_id": str(qid), "response": "not_for_me"})
        await ing.close_pool()

        import worker as worker_mod
        from conftest import FakeEmbedder
        from fixtures import FakeTMDB

        await worker_mod.Worker(pool, qdrant, FakeEmbedder(), tmdb=FakeTMDB()).run_once()
        async with pool.acquire() as conn:
            prof = get_vault().open_json(await conn.fetchval("SELECT profile FROM taste_profile WHERE user_id = $1 AND domain = 'video'", USER_A), P_PROFILE)
            status = await conn.fetchval("SELECT status FROM question_log WHERE id = $1", qid)
        await pool.close()
        return payload, r, prof, status

    payload, r, prof, status = asyncio.run(go())
    assert payload["prompt"].startswith("How do you feel") and payload["examples"]
    assert r.status_code == 202 and r.json()["events"] == 1 and status == "answered"
    assert prof["tag_weights"][payload["tag"]] < 0


def test_home_rows(db_dsn):
    use_service("recommendation-api")
    from app import rows as rows_mod
    from app.engine import Engine

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        eng = Engine(pool, qdrant)
        page = await rows_mod.build(eng, str(USER_A), "movie", per_row=6, max_rows=8)
        async with pool.acquire() as conn:
            surfaces = {r["surface"] for r in await conn.fetch("SELECT DISTINCT surface FROM impression WHERE user_id = $1", USER_A)}
            prof = get_vault().open_json(await conn.fetchval("SELECT profile FROM taste_profile WHERE user_id = $1 AND domain = 'video'", USER_A), P_PROFILE)
        await pool.close()
        return page, surfaces, prof

    page, surfaces, prof = asyncio.run(go())
    ids = [r["id"] for r in page["rows"]]
    assert ids[0] == "top"
    keys = [i["canon_key"] for r in page["rows"] for i in r["items"]]
    assert len(keys) == len(set(keys))  # a title appears once per page
    assert all(r["title"] and r["items"] for r in page["rows"])
    assert "rows:top" in surfaces
    # the 5-star Letterboxd film from 200 days ago is a rewatch candidate
    assert any(c["key"] == B("tmdb:movie:104") for c in prof["reconsume"])
    again = [r for r in page["rows"] if r["id"] == "again"]
    assert again and again[0]["items"][0]["reasons"][0].startswith("You loved it")
