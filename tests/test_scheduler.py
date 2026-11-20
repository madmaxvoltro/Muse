"""Scheduler jobs: weather, calendars (recurring events), webhooks (signed, retried), goals,
digest, retention."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT / "services" / "scheduler"))
pytest.importorskip("icalendar")
from scheduler import jobs as J  # noqa: E402

PERSON = uuid.UUID("66666666-6666-6666-6666-666666666666")
NOW = datetime(2026, 11, 23, 18, 30, tzinfo=timezone.utc)  # a Monday
ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:gym-1
DTSTART:20260105T180000Z
DTEND:20260105T193000Z
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Gym with Tom (leg day)
END:VEVENT
BEGIN:VEVENT
UID:hol
DTSTART;VALUE=DATE:20261123
DTEND;VALUE=DATE:20261124
SUMMARY:Vakantie
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
async def conn(db):
    c = await asyncpg.connect(db)
    yield c
    await c.close()


async def test_weather(conn, monkeypatch):
    monkeypatch.setattr(J, "WEATHER_LAT", "52.1")
    monkeypatch.setattr(J, "WEATHER_LON", "5.1")

    def api(req):
        assert req.url.params["latitude"] == "52.1"
        return httpx.Response(200, json={"current": {"temperature_2m": 9.5, "weather_code": 63, "is_day": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        v = await J.weather(conn, http)
    assert v == {"condition": "rain", "temp_c": 9.5, "is_day": False}
    assert json.loads(await conn.fetchval("SELECT value FROM context_signal WHERE kind = 'weather'"))["condition"] == "rain"


async def test_calendar_only_keeps_the_kind(conn):
    assert J.current_kind(ICS, NOW)[0] == "workout"  # weekly gym beats the all-day holiday
    assert J.current_kind(ICS, NOW + timedelta(hours=3))[0] == "holiday"
    await conn.execute("INSERT INTO calendar_feed (user_id, url) VALUES ($1, 'https://cal/x.ics')", PERSON)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=ICS))) as http:
        assert await J.calendars(conn, http, NOW) == 1
    v = json.loads(await conn.fetchval("SELECT value FROM context_signal WHERE kind = 'calendar' AND user_id = $1", PERSON))
    assert v == {"kind": "workout"}  # no title, no "Tom"


async def test_webhooks_signed_and_retried(conn):
    hook = await conn.fetchval("INSERT INTO webhook (url, secret, events) VALUES ('https://hook/x', 'shh', '{new_release}') RETURNING id")
    from muse_schema import notify

    assert await notify.emit(conn, "new_release", {"artist": "Kensington"}, PERSON, key="k1") == 1
    assert await notify.emit(conn, "new_release", {"artist": "Kensington"}, PERSON, key="k1") == 0  # announced once
    assert await notify.emit(conn, "digest", {}, PERSON) == 0  # not subscribed
    calls = []

    def receiver(req):
        calls.append(req)
        return httpx.Response(500 if len(calls) == 1 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as http:
        assert await J.deliver(conn, http) == {"delivered": 0, "failed": 1}
        await conn.execute("UPDATE webhook_delivery SET next_attempt_at = now()")
        assert await J.deliver(conn, http) == {"delivered": 1, "failed": 0}
    req = calls[-1]
    assert req.headers["X-Muse-Signature"] == J.signature("shh", req.content)
    assert json.loads(req.content)["data"]["artist"] == "Kensington"
    assert await conn.fetchval("SELECT failures FROM webhook WHERE id = $1", hook) == 0


async def test_goals_and_retention(conn):
    gid = await conn.fetchval("INSERT INTO goal (user_id, title, criteria, target, created_at) VALUES ($1, 'Explore jazz', $2::jsonb, 2, now() - interval '1 day') RETURNING id",
                              PERSON, json.dumps({"genres": ["jazz"]}))
    for i, g in enumerate(["jazz", "jazz", "metal"]):
        await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, metadata) "
                           "VALUES ($1, 'mono', $2, 'track', 'completed', 1.0, $3::jsonb)", PERSON, f"j{i}", json.dumps({"genres": [g]}))
    assert await J.goals(conn) == 1
    row = await conn.fetchrow("SELECT progress, status FROM goal WHERE id = $1", gid)
    assert (row["progress"], row["status"]) == (2, "done")

    await conn.execute("INSERT INTO retention_policy VALUES ('mono', 30)")
    await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, \"timestamp\") VALUES ($1, 'mono', 'old', 'track', 'played', now() - interval '40 days')", PERSON)
    out = await J.retention(conn)
    assert out["mono"] == 1


async def test_digest(conn):
    await conn.execute("INSERT INTO webhook (url, secret, events) VALUES ('https://hook/x', 's', '{digest}')")
    await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight) VALUES ($1, 'mono', 'a', 'track', 'completed', 1)", PERSON)
    await conn.execute("INSERT INTO question (user_id, prompt, kind) VALUES ($1, 'Which films do you love?', 'free_text')", PERSON)

    def rec(req):
        return httpx.Response(200, json={"recommendations": [{"source": "mono", "source_item_id": "b", "title": "X - Y", "why": ["Sounds like the music you love"]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as http:
        assert await J.digest(conn, http, NOW) == 1
        assert await J.digest(conn, http, NOW) == 0  # once per week
    payload = json.loads(await conn.fetchval("SELECT payload FROM webhook_delivery WHERE event = 'digest'"))
    assert payload["picks"]["track"][0]["why"] == "Sounds like the music you love" and payload["question"].startswith("Which films")
