"""Muse MCP server (optional): lets an assistant you already use (Open WebUI, Claude Desktop,
any MCP client) ask you Muse's questions in conversation, record your answers, and fetch
recommendations, your taste profile and how it's evolving. No interface of its own.

Nothing in Muse depends on this running. It's a thin client over Muse's APIs, in two modes:

* personal (recommended): MUSE_QUERY_URL + a person's token (MUSE_TOKEN, or the client's own
  `Authorization: Bearer` header over HTTP). Everything goes through the query API as that
  person: questions as simple choices, remembering what you tell the assistant, mood,
  context, goals, the taste portrait, ideas for Muse. Tokens come from an app (Mono's
  "connect an assistant") or POST /v1/tokens.
* household: the recommendation API (read key) and ingestion API /answers (the `muse` key),
  with a user_id per call. The muse_* tools below that take user_id.

Env:
  MUSE_RECO_URL       default http://recommendation-api:8001
  MUSE_INGEST_URL     default http://ingestion-api:8000
  MUSE_READ_KEY       X-Muse-Read-Key, if MUSE_READ_KEYS is set on the recommendation API
  MUSE_ANSWER_KEY     the `muse` key from MUSE_ADAPTER_KEYS (leave empty for a read-only server)
  MUSE_MCP_USER_ID    default Muse user (Jellyfin user GUID), so tools work without one
  MUSE_QUERY_URL      query API, e.g. http://query-api:8003 (personal mode)
  MUSE_TOKEN          a person's query API token (personal mode over stdio)
  MUSE_MCP_TRANSPORT  stdio (default) | streamable-http
  MUSE_MCP_HOST / MUSE_MCP_PORT   for streamable-http (default 127.0.0.1:8002, path /mcp)
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
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
QUERY_URL = os.environ.get("MUSE_QUERY_URL", "").rstrip("/")
TOKEN = _secret("MUSE_TOKEN")

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    "muse",
    instructions=(
        "Muse is the user's self-hosted media taste engine. Use muse_pending_questions now and then (not every turn) "
        "and ask the user one question naturally, in their language; record the answer with muse_answer_question, or "
        "muse_dismiss_question if they don't want to answer. Answering is always optional: never insist. "
        "muse_recommendations gives explained picks from their library; muse_taste_profile and muse_taste_evolution "
        "describe their taste and how it is changing. "
        "With a personal connection (muse_whoami works) prefer the me_* tools: me_questions to ask something "
        "(one at a time, in passing), me_answer with the option the user picked, me_remember whenever the user "
        "tells you something lasting about their taste ('I can't stand autotune', 'I love 70s Italian film "
        "music'), me_set_mood when they say how they feel or what they're doing, me_portrait to talk about their "
        "taste, me_suggest_feature when you notice Muse could do something it can't. Never invent preferences; "
        "only remember what the user said or clearly confirmed."
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
async def muse_home_rows(item_type: str = "movie", per_row: int = 6, user_id: str | None = None) -> dict:
    """A home screen of themed rows, each titled with its reason: Top picks, Because you watched
    X, More <interest>, For your weekend evenings, Watch again, From your watchlist, New in your
    library, Something different. Good for answering "what should I watch?" with a few options."""
    body = await _get(f"/rows/{_user(user_id)}", {"item_type": item_type, "per_row": max(4, min(per_row, 12)), "log": "false"})
    return {"rows": [{"title": r["title"], "items": [{k: i.get(k) for k in ("title", "year", "canon_key", "reasons")} for i in r["items"]]}
                     for r in body.get("rows", [])]}


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


# ---- personal mode: the query API as one person --------------------------------------------

def _token(ctx: Context | None) -> str:
    if ctx is not None:
        try:
            h = ctx.headers or {}
        except ValueError:  # called outside a request (tests, in-process use)
            h = {}
        auth = h.get("authorization") or h.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()  # the query API checks it; the MCP server only passes it on
    if TOKEN:
        return TOKEN
    raise ValueError("no personal connection: set MUSE_QUERY_URL and MUSE_TOKEN (or send Authorization: Bearer)")


async def _q(ctx: Context | None, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    if not QUERY_URL:
        raise ValueError("MUSE_QUERY_URL is not set (personal mode)")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.request(method, f"{QUERY_URL}{path}", json=body, params={k: v for k, v in (params or {}).items() if v is not None},
                            headers={"Authorization": f"Bearer {_token(ctx)}"})
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "detail": r.text[:500]}
    return r.json()


@mcp.tool(annotations=READ)
async def muse_whoami(ctx: Context) -> dict:
    """Who this connection acts for: the person, their linked accounts (Jellyfin, Mono, ...)."""
    return await _q(ctx, "GET", "/v1/me")


@mcp.tool(annotations=READ)
async def me_questions(ctx: Context, limit: int = 2, domain: str | None = None) -> dict:
    """Questions Muse would like the user to answer, as simple choices (prompt, options with a
    value and label, why Muse asks). Ask at most one, naturally, when it fits the conversation.
    domain: video | youtube | music | books."""
    return await _q(ctx, "GET", "/v1/users/me/questions", params={"limit": max(1, min(limit, 5)), "domain": domain})


@mcp.tool(annotations=WRITE)
async def me_answer(ctx: Context, question_id: str, option: str, note: str | None = None) -> dict:
    """Record the user's answer: `option` is the chosen option's value. `note`: anything extra
    the user said that's worth keeping (stored as a fact they can see and delete)."""
    return await _q(ctx, "POST", f"/v1/questions/{question_id}/answer", {"option": option, "text": note})


@mcp.tool(annotations=WRITE)
async def me_skip(ctx: Context, question_id: str) -> dict:
    """The user doesn't want to answer: close the question, nothing is learned."""
    return await _q(ctx, "POST", f"/v1/questions/{question_id}/skip", {})


@mcp.tool(annotations=WRITE)
async def me_remember(ctx: Context, statement: str, kind: str = "fact", domain: str = "general", confidence: float = 0.7) -> dict:
    """Tell Muse something lasting the user said about their taste or life. kind: likes
    (becomes part of their taste profile in that domain), dislikes, avoid (never suggest),
    context (circumstances, e.g. 'commutes by train'), fact. domain: video | youtube | music |
    books | general. Write `statement` as a short sentence in the user's own terms."""
    return await _q(ctx, "POST", "/v1/users/me/facts", {"statement": statement, "kind": kind, "domain": domain,
                                                        "confidence": max(0.0, min(confidence, 1.0))})


@mcp.tool(annotations=READ)
async def me_facts(ctx: Context, domain: str | None = None) -> dict:
    """What Muse was told about the user (by them or an assistant)."""
    return await _q(ctx, "GET", "/v1/users/me/facts", params={"domain": domain})


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False))
async def me_forget(ctx: Context, fact_id: str) -> dict:
    """Forget a fact (when the user says it's wrong or no longer true)."""
    return await _q(ctx, "DELETE", f"/v1/users/me/facts/{fact_id}")


@mcp.tool(annotations=WRITE)
async def me_set_mood(ctx: Context, mood: str, minutes: int = 180) -> dict:
    """The user's current mood or situation in a few words ('tired, rainy evening, want
    something gentle'). Recommendations use it until it expires."""
    return await _q(ctx, "POST", "/v1/users/me/context", {"kind": "mood", "value": {"mood": mood}, "ttl_minutes": max(5, min(minutes, 1440))})


@mcp.tool(annotations=WRITE)
async def me_set_context(ctx: Context, kind: str, value: dict, minutes: int = 120) -> dict:
    """Other context Muse may use: kind device | activity | company | weather | calendar | home,
    value e.g. {"activity": "cooking"} or {"company": ["partner"]}."""
    return await _q(ctx, "POST", "/v1/users/me/context", {"kind": kind, "value": value, "ttl_minutes": max(1, min(minutes, 10080))})


@mcp.tool(annotations=READ)
async def me_context(ctx: Context) -> dict:
    """The mood and context Muse currently uses for the user."""
    return await _q(ctx, "GET", "/v1/users/me/context")


@mcp.tool(annotations=READ)
async def me_recommendations(ctx: Context, item_type: str = "movie", limit: int = 8, mood: str | None = None) -> dict:
    """Explained picks for the user from their library. item_type: movie, series, youtube_video,
    track, audiobook, ebook. Without `mood` the stored mood (me_set_mood) is used."""
    return await _q(ctx, "GET", "/v1/users/me/recommendations", params={"item_type": item_type, "limit": max(1, min(limit, 30)), "mood": mood})


@mcp.tool(annotations=READ)
async def me_portrait(ctx: Context, item_type: str = "track") -> dict:
    """The user's taste in words for one domain (item_type track, movie, youtube_video,
    audiobook): summary, the interests that make it up, loved and disliked titles, trends."""
    return await _q(ctx, "GET", "/v1/users/me/taste-portrait", params={"item_type": item_type})


@mcp.tool(annotations=READ)
async def me_goals(ctx: Context) -> dict:
    """The user's goals with Muse (e.g. 'discover 20 new artists') and their progress."""
    return await _q(ctx, "GET", "/v1/users/me/goals")


@mcp.tool(annotations=WRITE)
async def me_add_goal(ctx: Context, title: str, target: int = 10, domain: str = "music") -> dict:
    """Set a goal the user asked for; progress = different titles finished since then."""
    return await _q(ctx, "POST", "/v1/users/me/goals", {"title": title, "target": max(1, target), "domain": domain})


@mcp.tool(annotations=WRITE)
async def me_incognito(ctx: Context, minutes: int = 60) -> dict:
    """Pause learning for a while (the user plays something for someone else, a party...). 0 ends it."""
    return await _q(ctx, "PUT", "/v1/users/me/incognito", {"minutes": max(0, min(minutes, 10080))})


@mcp.tool(annotations=WRITE)
async def me_suggest_feature(ctx: Context, title: str, description: str, rationale: str | None = None) -> dict:
    """An idea for Muse itself, when you notice it can't do something the user wanted."""
    return await _q(ctx, "POST", "/v1/suggestions", {"title": title, "description": description, "rationale": rationale})


def main() -> None:
    transport = os.environ.get("MUSE_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.run("streamable-http", host=os.environ.get("MUSE_MCP_HOST", "127.0.0.1"), port=int(os.environ.get("MUSE_MCP_PORT", "8002")))
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
