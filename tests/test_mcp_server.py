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
    assert set(tools) == {"muse_pending_questions", "muse_answer_question", "muse_dismiss_question", "muse_recommendations", "muse_taste_profile", "muse_taste_evolution", "muse_correct_reason"}
    assert tools["muse_pending_questions"].annotations.read_only_hint and not tools["muse_answer_question"].annotations.read_only_hint
    assert calls[0] == ("GET", "/questions/u1/queue", {"limit": 10, "domain": None})  # limit clamped, default user
    assert calls[1] == ("POST", "/answers", {"kind": "interest", "response": "more", "user_id": "u1", "question_id": "q1"})
    assert calls[2][2]["surface"] == "mcp" and json.loads(rec.content[0].text)["recommendations"][0]["title"] == "Heat"


def test_read_only_without_answer_key(monkeypatch):
    srv = _load(monkeypatch, answer_key="")
    with pytest.raises(ValueError):
        asyncio.run(srv._post_answer({"kind": "dismiss"}))
