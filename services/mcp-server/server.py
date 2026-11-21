"""Muse MCP server (optional): lets an assistant you already use (Open WebUI, Claude Desktop,
any MCP client) ask you Muse's questions in conversation, record your answers, and fetch
recommendations, your taste profile and how it's evolving. No interface of its own.

Nothing in Muse depends on this running. It's a thin client over the two APIs:
  reads  -> recommendation API (read key)
  writes -> ingestion API /answers (the `muse` key), nothing else

Env:
  MUSE_RECO_URL       default http://recommendation-api:8001
  MUSE_INGEST_URL     default http://ingestion-api:8000
  MUSE_READ_KEY       X-Muse-Read-Key, if MUSE_READ_KEYS is set on the recommendation API
  MUSE_ANSWER_KEY     the `muse` key from MUSE_ADAPTER_KEYS (leave empty for a read-only server)
  MUSE_MCP_USER_ID    default Muse user (Jellyfin user GUID), so tools work without one
  MUSE_MCP_TRANSPORT  stdio (default) | streamable-http
  MUSE_MCP_HOST / MUSE_MCP_PORT   for streamable-http (default 127.0.0.1:8002, path /mcp)
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

RECO_URL = os.environ.get("MUSE_RECO_URL", "http://recommendation-api:8001").rstrip("/")
INGEST_URL = os.environ.get("MUSE_INGEST_URL", "http://ingestion-api:8000").rstrip("/")
def _secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
    return os.environ.get(name, "")


READ_KEY = _secret("MUSE_READ_KEY")
ANSWER_KEY = _secret("MUSE_ANSWER_KEY")
DEFAULT_USER = os.environ.get("MUSE_MCP_USER_ID", "")

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    "muse",
    instructions=(
        "Muse is the user's self-hosted media taste engine. Use muse_pending_questions now and then (not every turn) "
        "and ask the user one question naturally, in their language; record the answer with muse_answer_question, or "
        "muse_dismiss_question if they don't want to answer. Answering is always optional: never insist. "
        "muse_recommendations gives explained picks from their library; muse_taste_profile and muse_taste_evolution "
        "describe their taste and how it is changing."
    ),
)


def _user(user_id: str | None) -> str:
    uid = user_id or DEFAULT_USER
    if not uid:
        raise ValueError("user_id is required (or set MUSE_MCP_USER_ID)")
    return uid


async def _get(path: str, params: dict[str, Any]) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(f"{RECO_URL}{path}", params={k: v for k, v in params.items() if v is not None},
                        headers={"X-Muse-Read-Key": READ_KEY} if READ_KEY else {})
        r.raise_for_status()
        return r.json()


async def _post_answer(body: dict) -> dict:
    if not ANSWER_KEY:
        raise ValueError("this Muse MCP server is read-only (MUSE_ANSWER_KEY not set)")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(f"{INGEST_URL}/answers", json=body, headers={"X-Muse-Api-Key": ANSWER_KEY})
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "detail": r.text[:500]}
        return {"ok": True, **r.json()}


@mcp.tool(annotations=READ)
async def muse_pending_questions(user_id: str | None = None, limit: int = 3, domain: str | None = None) -> dict:
    """Questions Muse would like to ask the user (most valuable first): onboarding picks,
    'why did you stop watching X?', this-or-that, and taste-evolution checks such as 'you seem
    to be getting into X, more of that?'. Each has a prompt, the allowed answers and the
    exact answer body. Answering is optional; unanswered questions expire harmlessly."""
    return await _get(f"/questions/{_user(user_id)}/queue", {"limit": max(1, min(limit, 10)), "domain": domain})


@mcp.tool(annotations=WRITE)
async def muse_answer_question(question_id: str, answer: dict, user_id: str | None = None) -> dict:
    """Record the user's answer. `answer` is the question's answer_with.body without user_id
    and question_id, e.g. {"kind": "interest", "response": "more"} or
    {"kind": "this_or_that", "a": "...", "b": "...", "answer": "a"} or
    {"kind": "abandon", "canon_key": "...", "reason": "not_in_mood"}."""
    return await _post_answer({**answer, "user_id": _user(user_id), "question_id": question_id})


@mcp.tool(annotations=WRITE)
async def muse_dismiss_question(question_id: str, user_id: str | None = None) -> dict:
    """The user doesn't want to answer: close the question without recording any taste data."""
    return await _post_answer({"kind": "dismiss", "user_id": _user(user_id), "question_id": question_id})


@mcp.tool(annotations=WRITE)
async def muse_correct_reason(reason_id: str, response: str, item_type: str = "movie", user_id: str | None = None) -> dict:
    """Correct a reason Muse gave for a recommendation (reason_details[].id). response:
    "not_relevant" (e.g. don't base recommendations on that title), "more" or "less" (an
    interest or a tag such as a director), or "restore" to undo. Use when the user says
    things like "I didn't even like that film" or "less of that director"."""
    return await _post_answer({"kind": "reason", "user_id": _user(user_id), "reason_id": reason_id, "response": response, "item_type": item_type})


@mcp.tool(annotations=READ)
async def muse_recommendations(item_type: str = "movie", limit: int = 10, mood: str | None = None, user_id: str | None = None) -> dict:
    """Explained recommendations from the user's library. item_type: movie, series,
    youtube_video, track, audiobook, ebook. mood: optional free text such as 'cosy and funny'."""
    body = await _get(f"/recommendations/{_user(user_id)}", {"item_type": item_type, "limit": max(1, min(limit, 30)), "mood": mood, "surface": "mcp"})
    return {
        "recommendations": [
            {k: r.get(k) for k in ("title", "year", "canon_key", "source", "source_item_id", "reasons", "reason_details", "interest", "explore", "confidence")}
            for r in body.get("recommendations", [])
        ],
        **({"note": body["note"]} if body.get("note") else {}),
    }


@mcp.tool(annotations=READ)
async def muse_taste_profile(domain: str = "video", user_id: str | None = None) -> dict:
    """What Muse thinks the user's taste is: interests with examples, loved and disliked titles,
    top tags and a summary. domain: video | youtube | music | books."""
    return await _get(f"/profile/{_user(user_id)}", {"domain": domain})


@mcp.tool(annotations=READ)
async def muse_taste_evolution(domain: str = "video", user_id: str | None = None) -> dict:
    """How the user's taste is changing: per interest emerging/growing/stable/fading/dormant,
    satiation, rhythms (when they watch what), a monthly timeline and milestones."""
    return await _get(f"/evolution/{_user(user_id)}", {"domain": domain})


def main() -> None:
    transport = os.environ.get("MUSE_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.run("streamable-http", host=os.environ.get("MUSE_MCP_HOST", "127.0.0.1"), port=int(os.environ.get("MUSE_MCP_PORT", "8002")))
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
