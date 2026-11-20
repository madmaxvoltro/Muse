"""End-to-end: events from Jellyfin (films) and Mono (music) -> embeddings and taste ->
linking the two accounts -> recommendations, facts, questions, search, tokens."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from qdrant_client import QdrantClient

from conftest import ROOT, FakeModel, load_module, load_package

_loaded: dict = {}


def services():
    if not _loaded:
        _loaded["worker"] = load_module("muse_worker", ROOT / "services" / "embedding-worker" / "worker.py")
        _loaded["rec"] = load_package("recapi", ROOT / "services" / "recommendation-api" / "app")
        load_module("recapi.main", ROOT / "services" / "recommendation-api" / "app" / "main.py")
        _loaded["query"] = load_package("queryapi", ROOT / "services" / "query-api" / "app")
        load_module("queryapi.main", ROOT / "services" / "query-api" / "app" / "main.py")
    import sys

    return _loaded["worker"], sys.modules["recapi.main"], sys.modules["queryapi.main"]


JF_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")  # Jellyfin GUID
MONO_USER = uuid.UUID("22222222-2222-2222-2222-222222222222")  # Mono account
OTHER = uuid.UUID("33333333-3333-3333-3333-333333333333")  # someone else in the house

TRACKS = {
    "t1": ("Kensington - Sorry", ["dutch rock", "dutch vocals"]),
    "t2": ("Kensington - Streets", ["dutch rock", "dutch vocals"]),
    "t3": ("Racoon - Oceaan", ["dutch rock", "dutch vocals", "ballad"]),
    "t4": ("Bløf - Harder dan ik hebben kan", ["dutch rock", "dutch vocals", "ballad"]),
    "t5": ("Metallica - Battery", ["thrash metal", "english vocals"]),
    "t6": ("Slayer - Angel of Death", ["thrash metal", "english vocals"]),
    "t7": ("Within Temptation - Ice Queen", ["symphonic metal", "dutch rock", "english vocals"]),
    "t8": ("Kensington - War", ["dutch rock", "dutch vocals"]),
}
MOVIES = {
    "m1": ("Alien", ["horror", "science fiction"]),
    "m2": ("The Thing", ["horror", "science fiction"]),
    "m3": ("Amelie", ["romance", "comedy"]),
    "m4": ("Interstellar", ["science fiction", "drama"]),
    "m5": ("Notting Hill", ["romance", "comedy"]),
}


async def insert(conn, user, source, item, item_type, action, weight, md, days_ago=1):
    await conn.execute(
        "INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, \"timestamp\", metadata) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)",
        user, source, item, item_type, action, weight, datetime.now(timezone.utc) - timedelta(days=days_ago), json.dumps(md),
    )


@pytest.fixture
async def world(db, monkeypatch):
    import asyncpg

    worker, rec, query = services()
    qd = QdrantClient(":memory:")
    worker.ensure_collections(qd)
    rec._qdrant = qd
    query.set_qdrant(qd)
    import sys

    sys.modules["recapi.db"]._pool = None
    sys.modules["queryapi.db"]._pool = None
    sys.modules["queryapi.embedder"].set_embedder(FakeModel())

    # query-api -> recommendation-api, in-process
    async def rec_call(user_id, item_type, **params):
        q = {"item_type": item_type, **{k: v for k, v in params.items() if v is not None}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rec.app), base_url="http://rec") as c:
            r = await c.get(f"/recommendations/{user_id}", params=q)
        if r.status_code >= 400:
            raise sys.modules["queryapi.recclient"].RecUnavailable(r.text)
        return r.json()

    monkeypatch.setattr(sys.modules["queryapi.recclient"], "recommendations", rec_call)

    conn = await asyncpg.connect(db)
    for tid, (title, genres) in TRACKS.items():
        await insert(conn, MONO_USER, "mono", tid, "track", "cataloged", 0, {"title": title, "genres": genres, "artist": title.split(" - ")[0]}, 30)
    for mid, (title, genres) in MOVIES.items():
        await insert(conn, JF_USER, "jellyfin", mid, "movie", "cataloged", 0, {"title": title, "genres": genres}, 30)
    md = lambda tid: {"title": TRACKS[tid][0], "genres": TRACKS[tid][1], "artist": TRACKS[tid][0].split(" - ")[0]}
    for tid in ("t1", "t2", "t3"):
        for _ in range(3):
            await insert(conn, MONO_USER, "mono", tid, "track", "completed", 1.0, md(tid))
    await insert(conn, MONO_USER, "mono", "t1", "track", "favorited", 1.5, md("t1"))
    await insert(conn, MONO_USER, "mono", "t5", "track", "skipped", -0.5, md("t5"))
    await insert(conn, JF_USER, "jellyfin", "m1", "movie", "completed", 1.0, {"title": "Alien", "genres": ["horror", "science fiction"]})
    await insert(conn, OTHER, "jellyfin", "m3", "movie", "completed", 1.0, {"title": "Amelie", "genres": ["romance", "comedy"]})
    await conn.close()

    pool = await sys.modules["queryapi.db"].get_pool()
    model = FakeModel()

    async def run_worker():
        await worker.run_once(pool, qd, model)

    await run_worker()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=query.app), base_url="http://query")
    yield {"client": client, "run_worker": run_worker, "qdrant": qd, "pool": pool, "rec": rec, "worker": worker}
    await client.aclose()
    await sys.modules["queryapi.db"].close_pool()
    await sys.modules["recapi.db"].close_pool()


ADMIN = {"Authorization": "Bearer admin-secret"}


def as_mono(user=MONO_USER):
    return {"X-Muse-Api-Key": "mono-key", "X-Muse-User": str(user)}


def as_jf(user=JF_USER):
    return {"X-Muse-Api-Key": "jf-key", "X-Muse-User": str(user)}


async def test_schema_accepts_mono_events():
    from muse_schema import Event

    e = Event(user_id=MONO_USER, source="mono", item_type="track", action="unfavorited", source_item_id="t1")
    assert e.source.value == "mono"


async def test_recommendations_exclude_heard_and_explain(world):
    c = world["client"]
    r = await c.get(f"/v1/users/{MONO_USER}/recommendations", params={"item_type": "track", "source": "mono"}, headers=as_mono())
    assert r.status_code == 200, r.text
    recs = r.json()["recommendations"]
    ids = [x["source_item_id"] for x in recs]
    assert "t1" not in ids and "t5" not in ids  # heard/judged
    assert ids[0] in ("t8", "t4")  # closest to Dutch rock taste
    assert recs[0]["why"] and recs[0]["title"]


async def test_adapter_cannot_act_for_other_sources_users(world):
    r = await world["client"].get(f"/v1/users/me/profile", headers=as_mono(JF_USER))
    assert r.status_code == 403


async def test_link_accounts_cross_domain_and_tokens(world):
    c = world["client"]
    # Jellyfin side makes a code, Mono side redeems it.
    code = (await c.post("/v1/identity/link-codes", headers=as_jf())).json()["code"]
    r = await c.post("/v1/identity/link", json={"code": code, "label": "Mono: max"}, headers=as_mono())
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == str(JF_USER)
    assert (await c.post("/v1/identity/link", json={"code": code}, headers=as_mono())).status_code == 400  # single use

    await world["run_worker"]()  # link marks the person dirty -> recomputed over both ids

    # A token for the person, minted by Mono (adapter), works as a bearer token for either id.
    tok = (await c.post("/v1/tokens", json={"label": "Claude", "display_name": "Max"}, headers=as_mono())).json()
    bearer = {"Authorization": f"Bearer {tok['token']}"}
    me = (await c.get("/v1/me", headers=bearer)).json()
    assert me["user_id"] == str(JF_USER) and me["display_name"] == "Max"
    assert {i["id"] for i in me["identities"]} == {str(JF_USER), str(MONO_USER)}

    prof = (await c.get("/v1/users/me/profile", headers=bearer)).json()
    assert "track" in prof["domains"] and "movie" in prof["domains"]
    assert prof["domains"]["track"]["top_artists"][0]["name"] == "Kensington"

    # Music taste for films: taste_from=track
    r = await c.get("/v1/users/me/recommendations", params={"item_type": "movie", "taste_from": "track"}, headers=bearer)
    assert r.status_code == 200 and r.json()["recommendations"]

    # The other housemate stays private.
    assert (await c.get(f"/v1/users/{OTHER}/profile", headers=bearer)).status_code == 403
    people = (await c.get("/v1/users", headers=ADMIN)).json()["users"]
    assert {p["user_id"] for p in people} == {str(JF_USER), str(OTHER)}

    # Unlink again.
    assert (await c.delete(f"/v1/identity/aliases/{MONO_USER}", headers=bearer)).status_code == 200
    me = (await c.get("/v1/me", headers=bearer)).json()
    assert {i["id"] for i in me["identities"]} == {str(JF_USER)}


async def test_facts_filter_and_shape_taste(world):
    c, qd = world["client"], world["qdrant"]
    r = await c.post(f"/v1/users/{MONO_USER}/facts", headers=as_mono(),
                     json={"statement": "Never suggest Bløf", "kind": "avoid", "subject": "Bløf", "domain": "track"})
    assert r.status_code == 200, r.text
    recs = (await c.get(f"/v1/users/{MONO_USER}/recommendations", params={"item_type": "track"}, headers=as_mono())).json()
    assert "t4" not in [x["source_item_id"] for x in recs["recommendations"]]

    r = await c.post(f"/v1/users/{MONO_USER}/facts", headers=as_mono(),
                     json={"statement": "Loves thrash metal like Slayer", "kind": "likes", "subject": "thrash metal",
                           "domain": "track", "sentiment": 1.0, "confidence": 1.0})
    recs = (await c.get(f"/v1/users/{MONO_USER}/recommendations", params={"item_type": "track"}, headers=as_mono())).json()
    t6 = next(x for x in recs["recommendations"] if x["source_item_id"] == "t6")
    assert t6["explanation"]["stated_preference"] > 0
    assert "You said you like thrash metal" in t6["why"]

    from muse_schema import taste_point_id

    before = qd.retrieve("taste_vectors", [taste_point_id(str(MONO_USER), "track")], with_vectors=True)[0]
    await world["run_worker"]()
    after = qd.retrieve("taste_vectors", [taste_point_id(str(MONO_USER), "track")], with_vectors=True)[0]
    assert after.payload["facts"] == 2 and list(after.vector) != list(before.vector)

    facts = (await c.get(f"/v1/users/{MONO_USER}/facts", headers=as_mono())).json()["facts"]
    fid = next(f["id"] for f in facts if f["kind"] == "avoid")
    assert (await c.delete(f"/v1/users/{MONO_USER}/facts/{fid}", headers=as_mono())).status_code == 200
    recs = (await c.get(f"/v1/users/{MONO_USER}/recommendations", params={"item_type": "track"}, headers=as_mono())).json()
    assert "t4" in [x["source_item_id"] for x in recs["recommendations"]]


async def test_questions_cold_start_answer_builds_taste(world):
    c, qd = world["client"], world["qdrant"]
    from muse_schema import taste_point_id

    qs = (await c.get(f"/v1/users/{MONO_USER}/questions", params={"limit": 10}, headers=as_mono())).json()["questions"]
    cold = next(q for q in qs if q["domain"] == "movie" and q["kind"] == "free_text" and "love" in q["prompt"])
    assert not qd.retrieve("taste_vectors", [taste_point_id(str(MONO_USER), "movie")])

    r = await c.post(f"/v1/questions/{cold['id']}/answer", json={"text": "Alien and The Thing, tense horror science fiction"}, headers=as_mono())
    assert r.status_code == 200, r.text
    assert r.json()["applied"][0].startswith("remembered: Loves these films")
    await world["run_worker"]()
    assert qd.retrieve("taste_vectors", [taste_point_id(str(MONO_USER), "movie")])  # built from the answer alone

    recs = (await c.get(f"/v1/users/{MONO_USER}/recommendations", params={"item_type": "movie"}, headers=as_mono())).json()
    assert recs["recommendations"][0]["source_item_id"] in ("m2", "m1")

    # Answered questions don't come back; someone else can't answer mine.
    again = (await c.get(f"/v1/users/{MONO_USER}/questions", params={"limit": 10}, headers=as_mono())).json()["questions"]
    assert cold["id"] not in [q["id"] for q in again]
    other_q = again[0]["id"]
    assert (await c.post(f"/v1/questions/{other_q}/answer", json={"text": "x"}, headers=as_jf(OTHER))).status_code == 403


async def test_item_feedback_question_rates_item(world):
    c = world["client"]
    qs = (await c.get(f"/v1/users/{MONO_USER}/questions", params={"limit": 20}, headers=as_mono())).json()["questions"]
    item_qs = [q for q in qs if q["kind"] == "item_feedback"]
    assert item_qs, qs
    q = item_qs[0]
    r = await c.post(f"/v1/questions/{q['id']}/answer", json={"option": "love"}, headers=as_mono())
    assert r.status_code == 200 and "rated the item 2" in r.json()["applied"]
    hist = (await c.get(f"/v1/users/{MONO_USER}/history", params={"action": "rated"}, headers=as_mono())).json()["events"]
    assert hist[0]["source_item_id"] == q["item"]["source_item_id"] and hist[0]["weight"] == 2.0


async def test_assistant_question_and_suggestions(world):
    c = world["client"]
    q = (await c.post(f"/v1/users/{MONO_USER}/questions", headers=as_mono(),
                      json={"prompt": "Do you listen to music while working?", "kind": "yes_no"})).json()
    r = await c.post(f"/v1/questions/{q['id']}/answer", json={"option": "yes", "note": "Mostly instrumental"}, headers=as_mono())
    assert r.status_code == 200
    facts = (await c.get(f"/v1/users/{MONO_USER}/facts", headers=as_mono())).json()["facts"]
    assert any("while working" in f["statement"] for f in facts) and any(f["statement"] == "Mostly instrumental" for f in facts)

    s1 = (await c.post("/v1/suggestions", headers=as_mono(), json={"title": "Mood playlists", "description": "Let me ask for a mood"})).json()
    s2 = (await c.post("/v1/suggestions", headers=as_mono(), json={"title": "mood playlists", "description": "again"})).json()
    assert s2["id"] == s1["id"] and s2["votes"] == 2


async def test_search_semantic_keyword_personalized(world):
    c = world["client"]
    r = (await c.get("/v1/search", params={"q": "kensington", "item_type": "track"}, headers=as_mono())).json()
    assert r["results"][0]["title"].startswith("Kensington") and "keyword" in r["mode"] and "semantic" in r["mode"]
    first = next(x for x in r["results"] if x["source_item_id"] == "t1")
    assert first["you"]["actions"]["favorited"] == 1

    r = (await c.get("/v1/search", params={"q": "dutch ballad"}, headers=as_mono())).json()
    assert {x["source_item_id"] for x in r["results"][:3]} & {"t3", "t4"}
    assert "personalized" in r["mode"]

    item = (await c.get("/v1/items/mono/t3", headers=as_mono())).json()
    assert item["title"] == "Racoon - Oceaan" and item["similar"]


async def test_settings_and_read_only_tokens(world):
    c = world["client"]
    tok = (await c.post("/v1/tokens", json={"label": "ro", "can_write": False}, headers=as_mono())).json()["token"]
    ro = {"Authorization": f"Bearer {tok}"}
    assert (await c.get("/v1/users/me/profile", headers=ro)).status_code == 200
    assert (await c.post("/v1/users/me/facts", json={"statement": "x"}, headers=ro)).status_code == 403
    s = (await c.put("/v1/users/me/settings", json={"explore_ratio": 0.3, "taste_blend": 0.2}, headers=as_mono())).json()
    assert s["explore_ratio"] == 0.3 and s["preferences"]["taste_blend"] == 0.2
    recs = (await c.get(f"/v1/users/{MONO_USER}/recommendations", params={"item_type": "track"}, headers=as_mono())).json()
    assert recs["recommendations"]


async def test_answers_about_other_sources_dont_lock_out_the_adapter(world):
    """A Mono user rating a Jellyfin film via Muse must still be usable by the Mono adapter."""
    c = world["client"]
    r = await c.post(f"/v1/users/{MONO_USER}/feedback", json={"source": "jellyfin", "source_item_id": "m2", "rating": 1}, headers=as_mono())
    assert r.status_code == 200, r.text
    assert (await c.get("/v1/users/me/profile", headers=as_mono())).status_code == 200
