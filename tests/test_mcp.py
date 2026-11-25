"""The MCP server, driven by a real MCP client, against the query API on the test database."""

from __future__ import annotations

import json
import sys

import httpx
import pytest

from muse_testkit import ROOT
from test_flow import MONO_USER, world  # noqa: F401  (fixture)

sys.path.insert(0, str(ROOT / "services" / "mcp-server"))
mcp_client = pytest.importorskip("mcp.client")
from muse_mcp import server as muse_mcp  # noqa: E402


def data(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
async def mcp(world, monkeypatch):
    query = sys.modules["queryapi.main"]
    tok = (await world["client"].post("/v1/tokens", json={"label": "test assistant"},
                                      headers={"X-Muse-Api-Key": "mono-key", "X-Muse-User": str(MONO_USER)})).json()["token"]
    monkeypatch.setattr(muse_mcp, "ENV_TOKEN", tok)
    monkeypatch.setattr(muse_mcp, "QUERY_URL", "http://query")
    monkeypatch.setattr(muse_mcp, "_transport", httpx.ASGITransport(app=query.app))
    # The client's task group must open and close in the same task: tests enter it themselves.
    return lambda: mcp_client.Client(muse_mcp.mcp)


async def test_tools_listed(mcp):
    async with mcp() as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"whoami", "search", "recommend", "get_questions", "answer_question", "remember", "suggest_feature",
                "create_link_code", "link_account", "get_taste_profile", "radio", "next_up", "search_by_sound", "get_taste_portrait",
                "new_releases", "wanted", "want", "set_mood", "set_activity", "start_group", "group_recommendations",
                "record_group_pick", "add_goal", "set_incognito", "forget_history", "why_not", "more_like_this"} <= names
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert {"taste_interview", "use_my_taste", "watch_together"} <= prompts


async def test_assistant_loop(mcp):
    async with mcp() as client:
        me = data(await client.call_tool("whoami", {}))
        assert me["user_id"] == str(MONO_USER)

        prof = data(await client.call_tool("get_taste_profile", {}))
        assert prof["domains"]["track"]["top_artists"][0]["name"] == "Kensington"

        qs = data(await client.call_tool("get_questions", {"limit": 5}))["questions"]
        q = next(q for q in qs if q["kind"] == "free_text" and q["domain"] == "movie")
        applied = data(await client.call_tool("answer_question", {"question_id": q["id"], "text": "Alien, The Thing"}))["applied"]
        assert applied

        fact = data(await client.call_tool("remember", {"statement": "Can't stand musicals", "kind": "avoid",
                                                      "subject": "musical", "domain": "movie"}))
        facts = data(await client.call_tool("list_facts", {}))["facts"]
        assert fact["id"] in [f["id"] for f in facts]

        hits = data(await client.call_tool("search", {"query": "kensington", "limit": 3}))["results"]
        assert hits and hits[0]["title"].startswith("Kensington")

        recs = data(await client.call_tool("recommend", {"item_type": "track", "limit": 3}))["recommendations"]
        assert recs and recs[0]["why"]

        code = data(await client.call_tool("create_link_code", {}))["code"]
        assert len(code) == 9

        s = data(await client.call_tool("suggest_feature", {"title": "Sleep timer awareness",
                                                          "description": "Know when audiobooks are used to fall asleep"}))
        assert s["votes"] == 1


async def test_errors_are_tool_errors(mcp):
    async with mcp() as client:
        r = await client.call_tool("get_taste_profile", {"user": "33333333-3333-3333-3333-333333333333"})
        assert r.is_error and "403" in r.content[0].text



async def test_new_tools(mcp):
    async with mcp() as client:
        mood = data(await client.call_tool("set_mood", {"mood": "focus", "hours": 1}))
        assert mood["value"]["mood"] == "focus"
        recs = data(await client.call_tool("recommend", {"item_type": "track", "limit": 3}))
        assert recs["context"]["mood"] == "focus"
        r = data(await client.call_tool("radio", {"seed_source": "mono", "seed_id": "t1", "length": 3, "playable_source": "mono"}))
        assert r["tracks"] and r["seed"] == "mono:t1"
        g = data(await client.call_tool("add_goal", {"title": "Get into Dutch rock", "domain": "track", "genres": ["dutch rock"]}))
        assert g["status"] == "active"
        assert data(await client.call_tool("why_not", {"source": "mono", "source_item_id": "t1"}))["blockers"]
        err = await client.call_tool("forget_history", {})
        assert err.is_error and "narrow it down" in err.content[0].text
        grp = data(await client.call_tool("start_group", {"members": ["33333333-3333-3333-3333-333333333333"], "name": "test"}))
        assert len(grp["members"]) == 2
