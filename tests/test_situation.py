"""Context signals, Home Assistant presence and concurrent group sessions."""

from __future__ import annotations

import httpx
import pytest

from test_flow import ADMIN, JF_USER, MONO_USER, OTHER, as_jf, as_mono, world  # noqa: F401


async def test_mood_and_activity(world):
    c = world["client"]
    r = await c.post("/v1/users/me/context", json={"kind": "mood", "value": {"mood": "rainy sunday, cooking for friends"}}, headers=as_mono())
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"mood": "rainy sunday, cooking for friends", "preset": False}  # vectors stay internal
    rec = world["rec"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rec.app), base_url="http://rec") as rc:
        ctx = (await rc.get(f"/context/{MONO_USER}", params={"tz": "Europe/Amsterdam"})).json()
    assert ctx["mood"] == "rainy sunday, cooking for friends" and ctx["derived_from"] == ["mood set by mono"]
    assert ctx["buckets"][1] in ("weekday", "weekend")
    recs = (await c.get(f"/v1/users/{MONO_USER}/recommendations", params={"item_type": "track"}, headers=as_mono())).json()
    assert recs["context"]["mood"].startswith("rainy")

    assert (await c.delete("/v1/users/me/context/mood", headers=as_mono())).json()["cleared"] == 1
    await c.post("/v1/users/me/context", json={"kind": "activity", "value": {"activity": "cooking"}}, headers=as_mono())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rec.app), base_url="http://rec") as rc:
        ctx = (await rc.get(f"/context/{MONO_USER}")).json()
    assert ctx["mood"] == "cooking" and ctx["derived_from"] == ["activity: cooking"]
    bad = await c.post("/v1/users/me/context", json={"kind": "activity", "value": {"activity": "juggling"}}, headers=as_mono())
    assert bad.status_code == 422


async def test_presence_makes_concurrent_groups(world):
    c = world["client"]
    for uid, ref in ((JF_USER, "person.max"), (OTHER, "person.sanne"), (MONO_USER, "person.kim")):
        await c.put(f"/v1/users/{uid}/refs/home_assistant", json={"ref": ref}, headers=ADMIN)
    # MONO_USER is its own person here (not linked), so three people, two rooms.
    living = (await c.post("/v1/integrations/home-assistant/presence", json={"room": "living room", "people": ["person.max", "person.sanne"]},
                           headers=ADMIN)).json()
    again = (await c.post("/v1/integrations/home-assistant/presence", json={"room": "living room", "people": ["person.sanne", "person.max"]},
                          headers=ADMIN)).json()
    assert living["group"]["id"] == again["group"]["id"] and set(living["group"]["members"]) == {str(JF_USER), str(OTHER)}
    kitchen = (await c.post("/v1/integrations/home-assistant/presence", json={"room": "kitchen", "people": ["person.kim", "person.ghost"]},
                            headers=ADMIN)).json()
    assert kitchen["group"] is None and kitchen["unknown"] == ["person.ghost"]  # one known person: no group

    manual = (await c.post("/v1/groups", json={"members": [str(OTHER)], "name": "Vesper"}, headers=as_mono())).json()
    assert set(manual["members"]) == {str(MONO_USER), str(OTHER)}  # the creator is always in it
    active = (await c.get("/v1/groups", headers=ADMIN)).json()["groups"]
    assert len(active) == 2  # living room + Vesper, at the same time

    recs = (await c.get(f"/v1/groups/{living['group']['id']}/recommendations", params={"item_type": "movie"}, headers=ADMIN)).json()
    assert set(recs["members"]) == {str(JF_USER), str(OTHER)}
    pick = (await c.post(f"/v1/groups/{living['group']['id']}/picks", json={"source": "jellyfin", "source_item_id": "m2"}, headers=ADMIN)).json()
    assert set(pick["fits"]) == {str(JF_USER), str(OTHER)}
    # Not your group: no access.
    assert (await c.get(f"/v1/groups/{living['group']['id']}/recommendations", headers=as_mono())).status_code == 403

    gone = (await c.post("/v1/integrations/home-assistant/presence", json={"room": "living room", "people": ["person.max"]}, headers=ADMIN)).json()
    assert gone["group"] is None
    assert len((await c.get("/v1/groups", headers=ADMIN)).json()["groups"]) == 1


async def test_calendars(world):
    c = world["client"]
    cal = (await c.post("/v1/users/me/calendars", json={"url": "webcal://cal.example/secret.ics", "label": "Personal"}, headers=as_mono())).json()
    assert "never titles" in cal["note"]
    assert (await c.get("/v1/users/me/calendars", headers=as_mono())).json()["calendars"][0]["label"] == "Personal"
    assert (await c.delete(f"/v1/users/me/calendars/{cal['id']}", headers=as_mono())).json()["deleted"] == 1
