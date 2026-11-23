"""Catalog worker against faked MusicBrainz / ListenBrainz / Deezer responses (real clients,
mock HTTP transport), on the test database."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, timedelta

import asyncpg
import httpx
import pytest

from muse_testkit import ROOT
from muse_schema.storage import open_event

sys.path.insert(0, str(ROOT / "services" / "catalog"))
from music_catalog import clients as C, worker as W  # noqa: E402
from test_flow import MONO_USER as MONO, world  # noqa: E402,F401

KEN = "11111111-aaaa-4aaa-8aaa-000000000001"
RACOON = "11111111-aaaa-4aaa-8aaa-000000000002"
BLOF = "11111111-aaaa-4aaa-8aaa-000000000003"
USER = uuid.UUID("44444444-4444-4444-4444-444444444444")
RECENT = (date.today() - timedelta(days=10)).isoformat()


def fake_api(request: httpx.Request) -> httpx.Response:
    u, p = request.url, dict(request.url.params)
    host, path = u.host, u.path
    j = lambda d: httpx.Response(200, json=d)  # noqa: E731
    if host == "musicbrainz.org":
        if path == "/ws/2/artist":
            name = p["query"].split('"')[1]
            ids = {"Kensington": KEN, "Racoon": RACOON, "Bløf": BLOF}
            return j({"artists": [{"id": ids[name], "name": name, "score": 100}] if name in ids else []})
        if path.startswith("/ws/2/artist/"):
            return j({"id": path.rsplit("/", 1)[1], "name": "x", "country": "NL",
                      "genres": [{"name": "Dutch Rock", "count": 5}, {"name": "pop rock", "count": 2}]})
        if path == "/ws/2/release-group":
            if p["artist"] == KEN:
                return j({"release-groups": [
                    {"id": "rg-old", "title": "Vultures", "primary-type": "Album", "first-release-date": "2012-03-01"},
                    {"id": "rg-new", "title": "Fortress", "primary-type": "Album", "first-release-date": RECENT},
                    {"id": "rg-live", "title": "Live", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": RECENT},
                ]})
            return j({"release-groups": []})
    if host == "labs.api.listenbrainz.org":
        if p["artist_mbids"] == KEN:
            return j([{"artist_mbid": RACOON, "name": "Racoon", "score": 900}, {"artist_mbid": BLOF, "name": "Bløf", "score": 800}])
        return j([])
    if host == "api.listenbrainz.org":
        return j([{"recording_mbid": "rec-oceaan", "recording_name": "Oceaan", "artist_name": "Racoon", "total_listen_count": 99}])
    if host == "api.deezer.com":
        if path == "/search/artist":
            return j({"data": [{"id": {"Kensington": 1, "Racoon": 2, "Bløf": 3}.get(p["q"], 9), "name": p["q"], "nb_fan": 10}]})
        if path.startswith("/artist/") and path.endswith("/top"):
            aid = path.split("/")[2]
            tracks = {"1": [("Sorry", 101), ("Streets", 102)], "2": [("Oceaan", 201), ("Brother", 202)], "3": [("Zoutelande", 301)]}[aid]
            name = {"1": "Kensington", "2": "Racoon", "3": "Bløf"}[aid]
            return j({"data": [{"id": i, "title": t, "artist": {"name": name}, "album": {"title": "A"}, "rank": 500,
                                "preview": f"https://cdn.example/{i}.mp3", "duration": 200} for t, i in tracks]})
        if path.startswith("/track/"):
            return j({"bpm": 120.0, "isrc": "NL123", "preview": "https://cdn.example/p.mp3"})
        if path.endswith("/related"):
            return j({"data": []})
    return httpx.Response(404, json={})


@pytest.fixture
async def conn(db):
    c = await asyncpg.connect(db)
    md = lambda t: json.dumps({"title": f"Kensington - {t}", "artist": "Kensington", "album": "Rivals"})  # noqa: E731
    for i, t in enumerate(["Sorry", "War"]):
        await c.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, metadata) "
                        "VALUES ($1, 'mono', $2, 'track', 'cataloged', 0, $3::jsonb)", USER, f"m{i}", md(t))
        await c.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, metadata) "
                        "VALUES ($1, 'mono', $2, 'track', 'completed', 1.0, $3::jsonb)", USER, f"m{i}", md(t))
    await c.execute("INSERT INTO webhook (user_id, url, secret, events) VALUES (NULL, 'http://hook', 's', '{*}')")
    yield c
    await c.close()


async def test_full_cycle(conn, db):
    http = C.Http(transport=httpx.MockTransport(fake_api))
    mb, lb, dz = C.MusicBrainz(http), C.ListenBrainz(http), C.Deezer(http)
    for t in (mb.throttle, lb.throttle, dz.throttle):
        t.gap = 0
    pool = await asyncpg.create_pool(db, min_size=1, max_size=2)

    async def rec_get(user, params):
        return [{"source": "catalog", "source_item_id": "mb:rec-oceaan", "score": 0.7, "why": ["Very close to what you enjoy"]}]

    stats = await W.run_cycle(pool, mb, lb, dz, rec_get)
    await pool.close()
    assert stats["seeds"] == 1

    artist = await conn.fetchrow("SELECT * FROM catalog_artist WHERE mbid = $1", KEN)
    assert json.loads(artist["tags"])[:1] == ["dutch rock"] and artist["deezer_id"] == 1
    assert {r["name"] for r in json.loads(artist["related"])} == {"Racoon", "Bløf"}

    # catalog events are stored like every event: blind id, sealed metadata (muse_schema.storage)
    items = {r["source_item_id"]: r["metadata"] for r in map(open_event, await conn.fetch(
        "SELECT source, source_item_id, metadata FROM event WHERE source = 'catalog'"))}
    assert "dz:101" not in items  # "Sorry" is already in the library
    assert items["mb:rec-oceaan"]["related_to"] == "Kensington" and items["mb:rec-oceaan"]["in_library"] is False
    assert items["dz:102"]["preview_url"] and "dutch rock" in items["dz:102"]["genres"]
    assert await conn.fetchval("SELECT count(*) FROM item_audio WHERE source = 'catalog'") == len(items)

    wanted = {r["dedupe_key"]: r for r in await conn.fetch("SELECT * FROM wanted")}
    assert "release:rg-new" in wanted and "release:rg-old" not in wanted and "release:rg-live" not in wanted
    assert wanted["release:rg-new"]["user_id"] == USER and wanted["release:rg-new"]["origin"] == "new_release"
    assert wanted["rec:mb:rec-oceaan"]["kind"] == "recording" and wanted["rec:mb:rec-oceaan"]["title"] == "Oceaan"
    events = [r["event"] for r in await conn.fetch("SELECT event FROM webhook_delivery")]
    assert events.count("new_release") == 1 and events.count("wanted") == 2

    # Second cycle: nothing announced twice; once the album is in Mono, the wanted row flips.
    await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, metadata) "
                       "VALUES ($1, 'mono', 'm9', 'track', 'cataloged', 0, $2::jsonb)", USER,
                       json.dumps({"title": "Kensington - Fortress", "artist": "Kensington", "album": "Fortress"}))
    await conn.execute("UPDATE catalog_artist SET releases_checked_at = now() - interval '2 days'")
    pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
    await W.run_cycle(pool, mb, lb, dz, rec_get)
    await pool.close()
    assert await conn.fetchval("SELECT count(*) FROM webhook_delivery WHERE event = 'new_release'") == 1
    assert await conn.fetchval("SELECT status FROM wanted WHERE dedupe_key = 'release:rg-new'") == "in_library"


def test_track_keys():
    assert W.track_key("Kensington", "Kensington - Sorry (Remastered 2020)") == W.track_key("Kensington feat. X", "Sorry")
    assert W.norm("Bløf") == W.norm("BLØF") and W.primary_artist("A feat. B") == "A"


async def test_wanted_api(conn, world):  # noqa: F811  (world from test_flow)
    c = world["client"]
    await conn.execute(
        "INSERT INTO catalog_release (mbid, artist_mbid, artist_name, title, primary_type, release_date) VALUES ('rg1', $1, 'Kensington', 'Fortress', 'Album', current_date - 3)", KEN)
    await conn.execute(
        "INSERT INTO wanted (user_id, kind, mbid, artist_mbid, artist_name, title, origin, score, dedupe_key) "
        "VALUES ($1, 'release', 'rg1', $2, 'Kensington', 'Fortress', 'new_release', 0.9, 'release:rg1')", MONO, KEN)
    h = {"X-Muse-Api-Key": "mono-key", "X-Muse-User": str(MONO)}
    rel = (await c.get("/v1/users/me/releases", headers=h)).json()["releases"]
    assert rel[0]["title"] == "Fortress" and not rel[0]["upcoming"]
    w = (await c.get("/v1/wanted", params={"user": "me"}, headers=h)).json()["wanted"]
    assert [x["title"] for x in w] == ["Fortress"]
    tok = (await c.post("/v1/tokens", json={"label": "lidarr", "can_write": False}, headers=h)).json()["token"]
    lid = (await c.get("/v1/wanted/lidarr", params={"token": tok})).json()
    assert lid == [{"MusicBrainzId": KEN, "ArtistMusicBrainzId": KEN, "ArtistName": "Kensington",
                    "AlbumMusicBrainzId": "rg1", "AlbumTitle": "Fortress"}]
    assert (await c.get("/v1/wanted/lidarr", params={"token": "nope"})).status_code == 401
    r = await c.put(f"/v1/wanted/{w[0]['id']}", json={"status": "grabbed", "external_id": "radarr-7"},
                    headers={"Authorization": "Bearer admin-secret"})
    assert r.json()["status"] == "grabbed"
    added = (await c.post("/v1/users/me/wanted", json={"artist": "Racoon", "title": "Oceaan"}, headers=h)).json()
    assert added["origin"] == "user"
