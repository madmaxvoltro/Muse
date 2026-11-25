"""The taste engine's MCP tools (questions queue, answers, reason corrections, home rows, profile,
evolution) inside the one MCP server (services/mcp-server/muse_mcp). The person comes from the
token (query API /v1/me); reads go to the recommendation API, answers to the ingestion API."""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "mcp-server"))


def _load(monkeypatch, answer_key="mk", read_key="rk"):
    monkeypatch.setenv("MUSE_ANSWER_KEY", answer_key)
    monkeypatch.setenv("MUSE_READ_KEY", read_key)
    monkeypatch.setenv("MUSE_TOKEN", "muse_test")
    import muse_mcp.server as server

    return importlib.reload(server)


TASTE_TOOLS = {"pending_taste_questions", "answer_taste_question", "dismiss_taste_question", "correct_reason", "home_rows",
               "taste_engine_profile", "taste_evolution"}


def test_tools_and_forwarding(monkeypatch):
    srv = _load(monkeypatch)
    calls = []

    async def fake_api(ctx, method, path, params=None, body=None):
        calls.append(("query", method, path))
        return {"kind": "user", "user_id": "u1"}

    async def fake_reco(path, params):
        calls.append(("GET", path, params))
        if path.startswith("/rows/"):
            return {"rows": [{"title": "Because you watched Heat", "items": [{"title": "Thief", "year": 1981, "canon_key": "k", "reasons": ["x"]}]}]}
        return {"questions": []}

    async def fake_answer(body):
        calls.append(("POST", "/answers", body))
        return {"ok": True}

    monkeypatch.setattr(srv, "_api", fake_api)
    monkeypatch.setattr(srv, "_reco", fake_reco)
    monkeypatch.setattr(srv, "_answer", fake_answer)

    async def go():
        tools = {t.name: t for t in await srv.mcp.list_tools()}
        await srv.mcp.call_tool("pending_taste_questions", {"limit": 50})
        await srv.mcp.call_tool("answer_taste_question", {"question_id": "q1", "answer": {"kind": "interest", "response": "more"}})
        rows = await srv.mcp.call_tool("home_rows", {"item_type": "series", "per_row": 2})
        await srv.mcp.call_tool("taste_evolution", {"user": "u9"})  # household token: explicit user, no /v1/me
        return tools, rows

    tools, rows = asyncio.run(go())
    assert TASTE_TOOLS <= set(tools)
    assert tools["pending_taste_questions"].annotations.read_only_hint and not tools["answer_taste_question"].annotations.read_only_hint
    assert calls[0] == ("query", "GET", "/v1/me")  # the person comes from the token
    assert calls[1] == ("GET", "/questions/u1/queue", {"limit": 10, "domain": None})  # limit clamped
    assert ("POST", "/answers", {"kind": "interest", "response": "more", "user_id": "u1", "question_id": "q1"}) in calls
    assert ("GET", "/rows/u1", {"item_type": "series", "per_row": 4, "log": "false"}) in calls  # per_row clamped, no impressions
    assert "Thief" in rows.content[0].text
    assert calls[-1] == ("GET", "/evolution/u9", {"domain": "video"})


def test_taste_tools_need_their_keys(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    srv = _load(monkeypatch, answer_key="", read_key="")
    with pytest.raises(ToolError):
        asyncio.run(srv._answer({"kind": "dismiss"}))
    with pytest.raises(ToolError):
        asyncio.run(srv._reco("/profile/u1", {}))
