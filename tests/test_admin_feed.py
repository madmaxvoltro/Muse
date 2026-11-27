"""The admin's feed, the curator controls and QR pairing, through the query API."""

from __future__ import annotations

import asyncpg

from muse_schema import notify
from test_flow import ADMIN, as_mono, world  # noqa: F401


async def _seed(db):
    conn = await asyncpg.connect(db)
    try:
        await conn.execute(
            "INSERT INTO curator_managed_item (source, source_item_id, item_type, estimated_bytes, arr_id) VALUES ('arr', '603', 'movie', 6442450944, '12')"
        )
        nid = await notify.admin(conn, "curator.soon_gone", "video", "The Matrix (1999) leaves in 48 hours", "Over budget.",
                                 severity="action", item_type="movie", source_item_id="603", actions=["keep", "remove_now"], key="t:1")
        again = await notify.admin(conn, "curator.soon_gone", "video", "dup", "dup", key="t:1")
        await notify.admin(conn, "music.new_release", "music", "New album", "From an artist you love.")
        await notify.admin(conn, "system.update", "system", "Update available", "./muse upgrade")
        await conn.execute(
            "INSERT INTO soon_gone (source_item_id, item_type, arr_id, expires_at) VALUES ('603', 'movie', '12', now() + interval '48 hours')"
        )
        sealed = await conn.fetchval("SELECT data::text FROM admin_notification WHERE id = $1::uuid", nid)
    finally:
        await conn.close()
    return nid, again, sealed


async def test_admin_feed_and_keep(world, db):
    c = world["client"]
    nid, again, sealed = await _seed(db)
    assert again is None  # same key: announced once
    assert "Matrix" not in sealed  # sealed at rest

    assert (await c.get("/v1/admin/notifications", headers=as_mono())).status_code == 403  # admin only
    video = (await c.get("/v1/admin/notifications", params={"domain": "video"}, headers=ADMIN)).json()
    assert [n["kind"] for n in video["notifications"]] == ["system.update", "curator.soon_gone"] and video["unread"] == 2
    music = (await c.get("/v1/admin/notifications", params={"domain": "music"}, headers=ADMIN)).json()
    assert {n["kind"] for n in music["notifications"]} == {"music.new_release", "system.update"}
    note = next(n for n in video["notifications"] if n["id"] == nid)
    assert note["title"].startswith("The Matrix") and note["actions"] == ["keep", "remove_now"] and note["item"] == {"type": "movie", "tmdb_id": "603"}

    r = await c.post(f"/v1/admin/notifications/{nid}/actions/keep", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {"kept": True, "was_on_soon_gone": True}
    assert (await c.post(f"/v1/admin/notifications/{nid}/actions/keep", headers=ADMIN)).status_code == 409

    status = (await c.get("/v1/admin/curator", headers=ADMIN)).json()
    assert status["counts"] == {"managed": 1, "pinned": 1, "soon_gone": 0}
    assert status["items"][0]["pinned"] and status["items"][0]["size_is_estimate"]
    assert status["activity"][0]["kind"] == "kept"

    assert (await c.post("/v1/admin/notifications/read-all", params={"domain": "video"}, headers=ADMIN)).json()["marked"] == 1  # system; keep already read its own
    assert (await c.get("/v1/admin/notifications", params={"domain": "music", "unread": "true"}, headers=ADMIN)).json()["unread"] == 1


async def test_curator_controls(world, db):
    c = world["client"]
    await _seed(db)
    r = await c.patch("/v1/admin/curator", json={"paused": True, "budget_gb": 500}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["paused"] and r.json()["budget_bytes"] == 500 * 1024**3
    assert (await c.post("/v1/admin/curator/items/movie/603/remove-now", headers=ADMIN)).json()["removing"]
    status = (await c.get("/v1/admin/curator", headers=ADMIN)).json()
    assert status["soon_gone"][0]["removing_now"] and status["run_requested"]
    assert (await c.post("/v1/admin/curator/items/series/1/keep", headers=ADMIN)).status_code == 404
    assert (await c.post("/v1/admin/curator/items/book/1/keep", headers=ADMIN)).status_code == 400


async def test_pairing_is_single_use_and_mints_tokens(world, db):
    c = world["client"]
    body = {"purpose": "vesper", "payload": {"reco_url": "https://muse.lan:8443/reco", "read_key": "vesper:abc"}, "admin_token": True}
    assert (await c.post("/v1/admin/pairing", json=body, headers=as_mono())).status_code == 403
    code = (await c.post("/v1/admin/pairing", json=body, headers=ADMIN)).json()["code"]
    conn = await asyncpg.connect(db)
    try:
        stored = await conn.fetchval("SELECT payload::text FROM pairing_code")
    finally:
        await conn.close()
    assert "vesper:abc" not in stored and code not in stored

    got = (await c.post("/v1/pair", json={"code": code, "device": "Pixel"})).json()
    assert got["reco_url"] == "https://muse.lan:8443/reco" and got["purpose"] == "vesper" and got["admin_token"].startswith("muse_")
    assert "_mint" not in got
    assert (await c.post("/v1/pair", json={"code": code})).status_code == 404  # single use
    # the minted token is an admin token
    assert (await c.get("/v1/admin/notifications", headers={"Authorization": f"Bearer {got['admin_token']}"})).status_code == 200
