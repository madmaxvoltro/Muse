import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "mcp-server"))


def _load(monkeypatch, answer_key="mk"):
    monkeypatch.setenv("MUSE_ANSWER_KEY", answer_key)
    monkeypatch.setenv("MUSE_MCP_USER_ID", "u1")
    import server

    return importlib.reload(server)


def test_tools_and_forwarding(monkeypatch):
    srv = _load(monkeypatch)
    calls = []

    async def fake_get(path, params):
        calls.append(("GET", path, params))
        return {"recommendations": [{"title": "Heat", "reasons": ["Because you like crime"], "score": 1.2, "explanation": {}}]} if "recommend" in path else {"questions": []}

    async def fake_post(body):
        calls.append(("POST", "/answers", body))
        return {"ok": True}

    monkeypatch.setattr(srv, "_get", fake_get)
    monkeypatch.setattr(srv, "_post_answer", fake_post)

    async def go():
        tools = {t.name: t for t in await srv.mcp.list_tools()}
        await srv.mcp.call_tool("muse_pending_questions", {"limit": 50})
        await srv.mcp.call_tool("muse_answer_question", {"question_id": "q1", "answer": {"kind": "interest", "response": "more"}})
        rec = await srv.mcp.call_tool("muse_recommendations", {"item_type": "series", "mood": "cosy"})
        return tools, rec

    tools, rec = asyncio.run(go())
    household = {"muse_pending_questions", "muse_answer_question", "muse_dismiss_question", "muse_recommendations", "muse_taste_profile", "muse_taste_evolution", "muse_correct_reason", "muse_home_rows"}
    personal = {"muse_whoami", "me_questions", "me_answer", "me_skip", "me_remember", "me_facts", "me_forget", "me_set_mood", "me_set_context",
                "me_context", "me_recommendations", "me_portrait", "me_goals", "me_add_goal", "me_incognito", "me_suggest_feature"}
    assert set(tools) == household | personal
    assert tools["muse_pending_questions"].annotations.read_only_hint and not tools["muse_answer_question"].annotations.read_only_hint
    assert calls[0] == ("GET", "/questions/u1/queue", {"limit": 10, "domain": None})  # limit clamped, default user
    assert calls[1] == ("POST", "/answers", {"kind": "interest", "response": "more", "user_id": "u1", "question_id": "q1"})
    assert calls[2][2]["surface"] == "mcp" and json.loads(rec.content[0].text)["recommendations"][0]["title"] == "Heat"


def test_read_only_without_answer_key(monkeypatch):
    srv = _load(monkeypatch, answer_key="")
    with pytest.raises(ValueError):
        asyncio.run(srv._post_answer({"kind": "dismiss"}))


def test_personal_mode_goes_through_the_query_api(monkeypatch):
    import httpx

    monkeypatch.setenv("MUSE_QUERY_URL", "http://query:8003")
    monkeypatch.setenv("MUSE_TOKEN", "muse_personal")
    srv = _load(monkeypatch)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params), request.headers.get("authorization"),
                     json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"ok": True, "questions": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def go():
        await srv.mcp.call_tool("me_questions", {"limit": 9, "domain": "music"})
        await srv.mcp.call_tool("me_answer", {"question_id": "q1", "option": "a", "note": "loves it on sundays"})
        await srv.mcp.call_tool("me_remember", {"statement": "Can't stand autotune", "kind": "dislikes", "domain": "music"})
        await srv.mcp.call_tool("me_set_mood", {"mood": "rainy evening", "minutes": 99999})
        await srv.mcp.call_tool("me_suggest_feature", {"title": "Lyrics search", "description": "find songs by a line"})

    asyncio.run(go())
    assert all(s[3] == "Bearer muse_personal" for s in seen)
    assert seen[0][:3] == ("GET", "/v1/users/me/questions", {"limit": "5", "domain": "music"})  # limit clamped
    assert seen[1][1] == "/v1/questions/q1/answer" and seen[1][4] == {"option": "a", "text": "loves it on sundays"}
    assert seen[2][1] == "/v1/users/me/facts" and seen[2][4]["kind"] == "dislikes"
    assert seen[3][4] == {"kind": "mood", "value": {"mood": "rainy evening"}, "ttl_minutes": 1440}
    assert seen[4][:2] == ("POST", "/v1/suggestions")
