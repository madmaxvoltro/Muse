import asyncio
import importlib
import json
import uuid

from conftest import use_service

USER = "33333333-3333-3333-3333-333333333333"


def _app(monkeypatch, dsn):
    monkeypatch.setenv("MUSE_DATABASE_URL", dsn)
    monkeypatch.setenv("MUSE_ADAPTER_KEYS", "jellyfin:jk,muse:mk,import:ik")
    use_service("ingestion-api")
    import app.auth
    import app.db
    import app.main

    importlib.reload(app.auth)
    importlib.reload(app.db)
    return importlib.reload(app.main)


def test_answers_imports_and_source_rules(db_dsn, monkeypatch):
    import asyncpg
    import httpx

    main = _app(monkeypatch, db_dsn)
    lb = "Date,Name,Year,Letterboxd URI,Rating\n2023-01-01,Arrival,2016,https://boxd.it/a,5\n2023-01-02,Meh,2010,https://boxd.it/b,3\n"

    async def go():
        t = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
            r1 = await c.post("/answers", headers={"X-Muse-Api-Key": "mk"}, json={"kind": "this_or_that", "user_id": USER, "a": "tmdb:movie:1", "b": "tmdb:movie:2", "answer": "a", "question_id": str(uuid.uuid4())})
            r2 = await c.post("/answers", headers={"X-Muse-Api-Key": "jk"}, json={"kind": "abandon", "user_id": USER, "canon_key": "tmdb:movie:3", "reason": "not_in_mood"})
            r3 = await c.post("/answers", headers={"X-Muse-Api-Key": "mk"}, json={"kind": "onboarding", "user_id": USER, "loved": ["tmdb:movie:4"], "disliked": ["tmdb:movie:5"]})
            imp = await c.post(f"/import/letterboxd?user_id={USER}&filename=ratings.csv", headers={"X-Muse-Api-Key": "ik"}, content=lb.encode())
            again = await c.post(f"/import/letterboxd?user_id={USER}&filename=ratings.csv", headers={"X-Muse-Api-Key": "ik"}, content=lb.encode())
            spoof = await c.post("/events", headers={"X-Muse-Api-Key": "jk"}, json={"user_id": USER, "source": "netflix", "item_type": "movie", "action": "played", "source_item_id": "x"})
            bad_import = await c.post("/events", headers={"X-Muse-Api-Key": "ik"}, json={"user_id": USER, "source": "jellyfin", "item_type": "movie", "action": "played", "source_item_id": "x"})
            batch = await c.post("/events/batch", headers={"X-Muse-Api-Key": "ik"}, json=[{"user_id": USER, "source": "spotify", "item_type": "track", "action": "played", "source_item_id": f"t{i}", "metadata": {"title": "s", "artist": "a"}} for i in range(5)])
            # the {"events": [...]} form Vesper and Mono send
            wrapped = await c.post("/events/batch", headers={"X-Muse-Api-Key": "jk"}, json={"events": [{"user_id": USER, "source": "jellyfin", "item_type": "movie", "action": "impression", "source_item_id": "v1"}]})
            assert wrapped.status_code == 202 and wrapped.json()["count"] == 1, wrapped.text
        await main.close_pool()
        conn = await asyncpg.connect(db_dsn)
        rows = await conn.fetch("SELECT source, action, action_weight, metadata FROM event ORDER BY ingested_at")
        await conn.close()
        return r1, r2, r3, imp, again, spoof, bad_import, batch, rows

    r1, r2, r3, imp, again, spoof, bad_import, batch, rows = asyncio.run(go())
    assert r1.status_code == 202 and r1.json()["events"] == 2
    assert r2.status_code == 403  # only the muse key may answer
    assert r3.status_code == 202 and r3.json()["events"] == 2
    assert imp.status_code == 202 and imp.json()["events"] == 2  # rated + watched
    assert again.status_code == 202
    assert spoof.status_code == 202 and bad_import.status_code == 403 and batch.status_code == 202
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    assert len(by_source["letterboxd"]) == 2  # second import deduplicated
    assert {r["action"] for r in by_source["muse"]} == {"chose", "passed_over", "rated"}
    from muse_schema.crypto import P_EVENT_METADATA, get_vault

    assert all(get_vault().open_json(r["metadata"], P_EVENT_METADATA)["canon_key"].startswith("tmdb:") for r in by_source["muse"])
    assert all("_enc" in json.loads(r["metadata"]) for r in rows)  # metadata sealed at rest
    assert not any("Arrival" in r["metadata"] or "boxd.it" in r["metadata"] for r in rows)
    assert "netflix" not in by_source and len(by_source["jellyfin"]) == 2  # server-side source wins (+ the wrapped batch)
    assert len(by_source["spotify"]) == 5
