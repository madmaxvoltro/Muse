"""A person's own data and controls, through the query API."""

from __future__ import annotations

import io
import json
import sys
import uuid
import zipfile

import asyncpg
import httpx
import pytest

from conftest import ROOT, load_package, load_module
from test_flow import ADMIN, MONO_USER, OTHER, as_mono, world  # noqa: F401

SPOTIFY = json.dumps([
    {"ts": "2025-03-01T21:00:00Z", "ms_played": 200000, "master_metadata_track_name": "Oceaan", "master_metadata_album_artist_name": "Racoon",
     "master_metadata_album_album_name": "Liverpool Rain", "spotify_track_uri": "spotify:track:abc", "reason_end": "trackdone", "skipped": False},
    {"ts": "2025-03-01T21:04:00Z", "ms_played": 9000, "master_metadata_track_name": "Battery", "master_metadata_album_artist_name": "Metallica",
     "spotify_track_uri": "spotify:track:def", "reason_end": "fwdbtn", "skipped": True},
    {"ts": "2025-03-01T21:05:00Z", "ms_played": 900000, "episode_name": "A podcast", "spotify_episode_uri": "spotify:episode:x"},
]).encode()
YOUTUBE = json.dumps([
    {"title": "Watched How synths work", "titleUrl": "https://www.youtube.com/watch?v=abc123XYZ", "subtitles": [{"name": "Synth School"}],
     "time": "2025-02-02T10:00:00.000Z"},
    {"title": "Watched Buy now", "titleUrl": "https://www.youtube.com/watch?v=ad", "details": [{"name": "From Google Ads"}], "time": "2025-02-02T10:00:00.000Z"},
]).encode()
GOODREADS = b"""Book Id,Title,Author,My Rating,Exclusive Shelf,Date Read,Date Added,ISBN13,Bookshelves
1,Dune,Frank Herbert,5,read,2024/05/01,2024/04/01,="9780441013593",sci-fi
2,Gone Girl,Gillian Flynn,0,to-read,,2024/06/01,,to-read
"""


def test_parsers():
    from importlib import import_module

    load_package("queryapi", ROOT / "services" / "query-api" / "app")
    imp = load_module("queryapi.importers", ROOT / "services" / "query-api" / "app" / "importers.py")
    sp = imp.parse("spotify", [("Streaming_History_Audio_2025.json", SPOTIFY)])
    assert [(e["action"], e["metadata"]["title"]) for e in sp] == [("completed", "Racoon - Oceaan"), ("skipped", "Metallica - Battery")]
    yt = imp.parse("youtube", [("watch-history.json", YOUTUBE)])
    assert len(yt) == 1 and yt[0]["source_item_id"] == "abc123XYZ" and yt[0]["metadata"]["title"] == "How synths work"
    gr = imp.parse("goodreads", [("goodreads_library_export.csv", GOODREADS)])
    assert [(e["action"], e["action_weight"]) for e in gr] == [("rated", 2.0), ("added_watchlist", None)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2025.json", SPOTIFY)
        z.writestr("Spotify Extended Streaming History/ReadMeFirst.pdf.json", b"[]")
    assert len(imp.parse("spotify", [("my_spotify_data.zip", buf.getvalue())])) == 2
    sc = imp.parse("soundcloud", [("likes.csv", b"Title,Artist,Created At\nNight Drive,Kavinsky,2024-01-02\n")])
    assert sc[0]["action"] == "favorited" and sc[0]["metadata"]["title"] == "Kavinsky - Night Drive"


async def test_import_export_forget(world):
    c = world["client"]
    r = await c.post("/v1/users/me/imports/spotify", files={"files": ("Streaming_History_Audio_2025.json", SPOTIFY)}, headers=as_mono())
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 2
    again = (await c.post("/v1/users/me/imports/spotify", files={"files": ("Streaming_History_Audio_2025.json", SPOTIFY)}, headers=as_mono())).json()
    assert again["added"] == 0 and again["already_imported"] == 2
    assert (await c.get("/v1/users/me/profile", headers=as_mono())).status_code == 200  # the import doesn't lock out the Mono adapter

    exp = (await c.get("/v1/users/me/export", headers=as_mono())).json()
    assert any(e["source"] == "spotify" for e in exp["events"]) and exp["linked_ids"] == [str(MONO_USER)]
    assert exp["access_log"]  # the export call itself and the profile read are logged
    log = (await c.get("/v1/users/me/access-log", headers=as_mono())).json()["entries"]
    assert any(e["path"].endswith("/export") and e["by"] == "adapter:mono" for e in log)

    assert (await c.delete("/v1/users/me/events", params={"source": "spotify"}, headers=as_mono())).status_code == 400  # needs confirm
    gone = (await c.delete("/v1/users/me/events", params={"source": "spotify", "confirm": "true"}, headers=as_mono())).json()
    assert gone["deleted_events"] == 2


async def test_incognito_stops_learning(world, db):
    c = world["client"]
    assert (await c.put("/v1/users/me/incognito", json={"minutes": 60}, headers=as_mono())).json()["incognito_until"]
    ing = load_package("ingestapi", ROOT / "services" / "ingestion-api" / "app")
    main = load_module("ingestapi.main", ROOT / "services" / "ingestion-api" / "app" / "main.py")
    sys.modules["ingestapi.db"]._pool = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://ing") as ic:
        body = {"user_id": str(MONO_USER), "source": "mono", "source_item_id": "t2", "item_type": "track", "action": "played"}
        r = await ic.post("/events", json=body, headers={"X-Muse-Api-Key": "mono-key"})
        assert r.json() == {"status": "dropped", "reason": "incognito"}
        await c.put("/v1/users/me/incognito", json={"minutes": 0}, headers=as_mono())
        main._incognito.clear()
        assert (await ic.post("/events", json=body, headers={"X-Muse-Api-Key": "mono-key"})).json()["status"] == "accepted"
    await sys.modules["ingestapi.db"].close_pool()


async def test_feedback_and_why_not(world):
    c = world["client"]
    more = (await c.post("/v1/users/me/more-like-this", json={"source": "mono", "source_item_id": "t3"}, headers=as_mono())).json()
    assert more["fact"]["statement"] == "More like Racoon - Oceaan"
    why = (await c.get("/v1/users/me/why-not/mono/t1", headers=as_mono())).json()
    assert not why["recommendable"] and why["blockers"][0].startswith("already played")
    await c.post(f"/v1/users/{MONO_USER}/facts", headers=as_mono(), json={"statement": "Never suggest Bløf", "kind": "avoid", "subject": "Bløf", "domain": "track"})
    why = (await c.get("/v1/users/me/why-not/mono/t4", headers=as_mono())).json()
    assert any("Never suggest Bløf" in b for b in why["blockers"])


async def test_webhooks_goals_review_portrait(world, db):
    c = world["client"]
    hook = (await c.post("/v1/webhooks", json={"url": "https://example/hook", "events": ["new_release", "digest"]}, headers=as_mono())).json()
    assert hook["secret"].startswith("whsec_")
    assert (await c.post("/v1/webhooks", json={"url": "https://x", "events": ["*"], "user": None}, headers=as_mono())).status_code == 403
    assert len((await c.get("/v1/webhooks", headers=as_mono())).json()["webhooks"]) == 1

    goal = (await c.post("/v1/users/me/goals", json={"title": "Get into Dutch rock", "domain": "track", "genres": ["dutch rock"], "target": 3},
                         headers=as_mono())).json()
    assert goal["criteria"]["genres"] == ["dutch rock"] and "vec" not in goal["criteria"]

    review = (await c.get("/v1/users/me/year-review", headers=as_mono())).json()
    assert review["domains"]["track"]["top_artists"][0] == "Kensington"

    conn = await asyncpg.connect(db)
    await conn.execute("INSERT INTO taste_facet (user_id, item_type, space, idx, share, size, label, descriptors, examples, features) VALUES "
                       "($1, 'track', 'audio', 0, 0.6, 20, 'melancholic, piano, slow tempo', '[\"melancholic\"]', '[{\"title\": \"Racoon - Oceaan\"}]', '{\"bpm\": 72}'), "
                       "($1, 'track', 'audio', 1, 0.4, 14, 'aggressive, distorted electric guitars', '[]', '[]', '{}')", MONO_USER)
    await conn.execute("INSERT INTO taste_model (user_id, item_type, kind, model, metrics) VALUES ($1, 'track', 'audio_pref', $2, '{\"auc\": 0.8}')",
                       MONO_USER, json.dumps({"summary": ["prefers acoustic, organic sound"]}))
    await conn.close()
    portrait = (await c.get("/v1/users/me/taste-portrait", headers=as_mono())).json()["domains"]["track"]
    assert portrait["summary"].startswith("2 sides to your music: melancholic, piano, slow tempo (60%)")
    assert portrait["summary"].endswith("Within that you prefer acoustic, organic sound.")

    full = (await c.delete("/v1/users/me", params={"confirm": "true"}, headers=as_mono())).json()
    assert full["rows"]["event"] > 0 and full["rows"]["webhook"] == 1
    assert (await c.get("/v1/users/me/facts", headers=as_mono())).json()["facts"] == []
