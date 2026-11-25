"""The query API end to end, against the real recommendation and ingestion APIs: a Mono account
linked to a Jellyfin account becomes one person, facts steer the taste profile, questions are
served as cards and answered, a stored mood reaches recommendations, incognito drops events,
goals count progress, and everything free text is sealed in the database."""

import asyncio
import importlib
import sys
import uuid

import httpx

from conftest import ROOT, FakeEmbedder, use_service
from fixtures import FakeTMDB, USER_A, insert
from muse_schema.crypto import P_QUESTION, get_vault
from test_music import MAX, md
from test_music import seed as seed_music
from test_worker import run_worker

ADMIN = "admin-token-for-tests"


def test_query_api(db_dsn, monkeypatch):
    monkeypatch.setenv("MUSE_DATABASE_URL", db_dsn)
    monkeypatch.setenv("MUSE_ADAPTER_KEYS", "muse:mk,mono:monok,jellyfin:jfk")
    monkeypatch.setenv("MUSE_ANSWER_KEY", "mk")
    monkeypatch.setenv("MUSE_ADMIN_TOKEN", ADMIN)
    import worker as worker_mod

    async def go():
        pool, qdrant, _ = await run_worker(db_dsn)
        async with pool.acquire() as conn:
            await seed_music(conn)  # MAX: a Mono account with a music history
        w = worker_mod.Worker(pool, qdrant, FakeEmbedder(), llm=None, tmdb=FakeTMDB())
        await w.run_once()

        use_service("recommendation-api")
        from app import main as reco_main
        from app.engine import Engine

        reco_main._engine = Engine(pool, qdrant, FakeEmbedder)
        reco_app = reco_main.app
        use_service("ingestion-api")
        import app.auth
        import app.db
        import app.main as ing_main

        importlib.reload(app.auth)
        importlib.reload(app.db)
        ing_main = importlib.reload(ing_main)
        sys.path.insert(0, str(ROOT / "services" / "query-api"))
        from query import clients, db as qdb, main as q

        qdb.set_pool(pool)
        clients.use_transports(httpx.ASGITransport(app=reco_app), httpx.ASGITransport(app=ing_main.app))
        out: dict = {}
        mono = {"X-Muse-Api-Key": "monok", "X-Muse-User": str(MAX)}
        jelly = {"X-Muse-Api-Key": "jfk", "X-Muse-User": str(USER_A)}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=q.app), base_url="http://q") as c, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=ing_main.app), base_url="http://i") as ing:
            out["anon"] = (await c.get("/v1/me")).status_code
            out["spoof"] = (await c.get("/v1/me", headers={"X-Muse-Api-Key": "monok", "X-Muse-User": str(USER_A)})).status_code
            out["me_before"] = (await c.get("/v1/me", headers=mono)).json()

            # link: code made as the Jellyfin account, redeemed as the Mono account
            code = (await c.post("/v1/identity/link-codes", headers=jelly)).json()["code"]
            out["link"] = (await c.post("/v1/identity/link", headers=mono, json={"code": code.lower(), "label": "Mono: max"})).json()
            out["reuse"] = (await c.post("/v1/identity/link", headers=mono, json={"code": code})).status_code
            out["me"] = (await c.get("/v1/me", headers=mono)).json()

            # facts and tokens
            fact = (await c.post("/v1/users/me/facts", headers=mono,
                                 json={"statement": "loves slow piano ballad strings", "kind": "likes", "domain": "track"})).json()
            out["fact"] = fact
            out["facts"] = (await c.get("/v1/users/me/facts", headers=jelly)).json()["facts"]
            tok = (await c.post("/v1/tokens", headers=mono, json={"label": "Mono: max assistant", "display_name": "Max"})).json()
            bearer = {"Authorization": f"Bearer {tok['token']}"}
            out["token_me"] = (await c.get("/v1/me", headers=bearer)).json()
            out["tokens"] = (await c.get("/v1/tokens", headers=mono)).json()["tokens"]
            out["admin_users"] = (await c.get("/v1/users", headers={"Authorization": f"Bearer {ADMIN}"})).json()["users"]
            out["not_admin"] = (await c.get("/v1/users", headers=bearer)).status_code

            # a this-or-that question for the person, shown as a card and answered with an option
            async with pool.acquire() as conn:
                keys = [r["canon_key"] for r in await conn.fetch("SELECT canon_key FROM canon_item WHERE item_type = 'track' LIMIT 2")]
                qid = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO question_log (id, user_id, kind, payload, status, domain, priority, dedupe_key, expires_at)
                       VALUES ($1, $2, 'this_or_that', $3::jsonb, 'pending', 'music', 0.99, 'pair:test', now() + interval '1 day')""",
                    qid, USER_A, get_vault().seal_json({"prompt": "Which would you rather hear?", "answers": ["a", "b", "both", "neither", "skip"],
                                                        "a": {"canon_key": keys[0], "title": "Song A"}, "b": {"canon_key": keys[1], "title": "Song B"}}, P_QUESTION),
                )
            out["questions"] = (await c.get("/v1/users/me/questions", headers=mono, params={"limit": 5})).json()["questions"]
            out["bad_option"] = (await c.post(f"/v1/questions/{qid}/answer", headers=mono, json={"option": "c"})).status_code
            out["answer"] = (await c.post(f"/v1/questions/{qid}/answer", headers=mono, json={"option": "a", "text": "Song A is my sunday song"})).json()
            out["others_q"] = (await c.post(f"/v1/questions/{uuid.uuid4()}/skip", headers=mono)).status_code

            # mood -> recommendations; context signals
            await c.post("/v1/users/me/context", headers=mono, json={"kind": "mood", "value": {"mood": "calm piano evening"}, "ttl_minutes": 60})
            await c.post("/v1/users/me/context", headers=mono, json={"kind": "device", "value": {"device": "car"}, "ttl_minutes": 30})
            out["context"] = (await c.get("/v1/users/me/context", headers=mono)).json()["signals"]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reco_app), base_url="http://r") as r:
                out["recs"] = (await r.get(f"/recommendations/{MAX}", params={"item_type": "track", "limit": 3, "log": "false"})).json()
            await c.delete("/v1/users/me/context/mood", headers=mono)
            out["context_after"] = (await c.get("/v1/users/me/context", headers=mono)).json()["signals"]

            # incognito: Mono's listens are dropped while it lasts
            out["incognito"] = (await c.put("/v1/users/me/incognito", headers=mono, json={"minutes": 30})).json()
            out["settings"] = (await c.get("/v1/users/me/settings", headers=mono)).json()
            async with pool.acquire() as conn:
                before = await conn.fetchval("SELECT count(*) FROM event WHERE user_id = $1", MAX)
            ev = {"user_id": str(MAX), "source": "mono", "source_item_id": "metal7", "item_type": "track", "action": "completed",
                  "timestamp": "2026-11-20T10:00:00Z", "progress_pct": 100, "metadata": md("metal", 7)}
            out["ingest"] = (await ing.post("/events", headers={"X-Muse-Api-Key": "monok"}, json=ev)).status_code
            async with pool.acquire() as conn:
                out["dropped"] = await conn.fetchval("SELECT count(*) FROM event WHERE user_id = $1", MAX) == before
            await c.put("/v1/users/me/incognito", headers=mono, json={"minutes": 0})
            ing_main._INCOGNITO.clear()
            await ing.post("/events", headers={"X-Muse-Api-Key": "monok"}, json=ev)
            async with pool.acquire() as conn:
                out["kept"] = await conn.fetchval("SELECT count(*) FROM event WHERE user_id = $1", MAX) == before + 1

            # goals
            out["goal"] = (await c.post("/v1/users/me/goals", headers=mono, json={"title": "Discover 2 new songs", "target": 2})).json()
            async with pool.acquire() as conn:
                await insert(conn, MAX, "mono", "dance8", "track", "completed", 0, 1.0, 100, md("dance", 8))
                await insert(conn, MAX, "mono", "dance9", "track", "completed", 0, 1.0, 100, md("dance", 9))
            out["goals"] = (await c.get("/v1/users/me/goals", headers=mono)).json()["goals"]

            # the worker picks up the link and the fact; the portrait reads the merged profile
            await w.run_once()
            out["portrait"] = (await c.get("/v1/users/me/taste-portrait", headers=bearer, params={"item_type": "track"})).json()
            out["releases"] = (await c.get("/v1/users/me/releases", headers=mono)).json()
            out["forget"] = (await c.delete(f"/v1/users/me/facts/{fact['id']}", headers=mono)).status_code
            out["facts_after"] = (await c.get("/v1/users/me/facts", headers=mono)).json()["facts"]
            async with pool.acquire() as conn:
                out["stored"] = [str(v) for v in await conn.fetch("SELECT statement::text FROM profile_fact")] + \
                    [str(await conn.fetchval("SELECT mood::text FROM user_context")), str(await conn.fetchval("SELECT label::text FROM user_alias"))]
                out["token_rows"] = [r["token_hash"] for r in await conn.fetch("SELECT token_hash FROM api_token")]
            out["revoke"] = (await c.delete(f"/v1/tokens/{tok['id']}", headers=mono)).status_code
            out["revoked_me"] = (await c.get("/v1/me", headers=bearer)).status_code
            alias = next(i["id"] for i in out["me"]["identities"] if not i["canonical"])
            out["unlink"] = (await c.delete(f"/v1/identity/aliases/{alias}", headers=jelly)).status_code
            out["me_unlinked"] = (await c.get("/v1/me", headers=mono)).json()
            out["token_value"] = tok["token"]
        await ing_main.close_pool()
        reco_main._engine = None
        qdb.set_pool(None)
        clients.use_transports()
        await pool.close()
        return out

    out = asyncio.run(go())
    assert out["anon"] == 401 and out["spoof"] == 403  # Mono can't act for a Jellyfin user
    assert out["me_before"]["user_id"] == str(MAX) and len(out["me_before"]["identities"]) == 1
    assert out["link"]["to"] == str(USER_A) and out["link"]["linked"] == str(MAX) and out["reuse"] == 404
    me = out["me"]
    assert me["user_id"] == str(USER_A) and {i["id"] for i in me["identities"]} == {str(USER_A), str(MAX)}
    alias = next(i for i in me["identities"] if i["id"] == str(MAX))
    assert alias["label"] == "Mono: max" and alias["events_by_source"].get("mono", 0) > 0

    assert out["fact"]["domain"] == "music" and out["facts"][0]["statement"] == "loves slow piano ballad strings"
    assert out["token_me"]["user_id"] == str(USER_A) and out["token_me"]["kind"] == "person"
    assert any(t["label"] == "Mono: max assistant" for t in out["tokens"])
    people = {u["user_id"]: u for u in out["admin_users"]}
    assert str(MAX) not in people and people[str(USER_A)]["linked_ids"] == [str(MAX)]
    assert out["not_admin"] == 403

    card = next(q for q in out["questions"] if q["muse_kind"] == "this_or_that")
    assert card["kind"] == "choice" and card["prompt"] and [o["value"] for o in card["options"]] == ["a", "b", "both", "neither"]
    assert card["options"][0]["label"] == "Song A" and card["reason"]
    assert out["bad_option"] == 422 and out["answer"]["events"] == 2 and out["answer"]["fact"]["kind"] == "fact"
    assert out["others_q"] == 404

    kinds = {s["kind"]: s for s in out["context"]}
    assert kinds["mood"]["value"] == {"mood": "calm piano evening"} and kinds["device"]["value"] == {"device": "car"}
    assert out["recs"]["user_id"] == str(USER_A) and out["recs"]["mood"] == "calm piano evening" and out["recs"]["recommendations"]
    assert "mood" not in {s["kind"] for s in out["context_after"]}

    assert out["incognito"]["incognito_until"] and out["settings"]["preferences"]["incognito_until"]
    assert out["ingest"] == 202 and out["dropped"] and out["kept"]

    assert out["goal"]["progress"] == 0 and out["goal"]["target"] == 2
    assert out["goals"][0]["progress"] >= 2 and out["goals"][0]["status"] == "done"

    track = out["portrait"]["domains"]["track"]
    assert isinstance(track["summary"], str) and track["sound"]
    assert any(s["label"] == "loves slow piano ballad strings" for s in track["sound"]), track["sound"]  # told, now an interest
    assert out["releases"]["releases"] == []
    assert out["forget"] == 200 and all(f["id"] != out["fact"]["id"] for f in out["facts_after"])

    if get_vault().enabled:  # nothing readable at rest
        for v in out["stored"]:
            assert "piano" not in v and "sunday" not in v and "Mono" not in v
    assert out["token_value"] not in out["token_rows"]
    assert out["revoke"] == 200 and out["revoked_me"] == 401
    assert out["unlink"] == 200 and out["me_unlinked"]["user_id"] == str(MAX)
