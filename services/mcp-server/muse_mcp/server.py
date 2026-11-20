"""Muse MCP server: lets an AI assistant know, search and teach a person's taste.

It is a thin client of the Muse Query API (services/query-api), so everything it can do is
bounded by the token it runs with:

* a **personal token** acts for one person (and the accounts they linked);
* a **household token** (or MUSE_ADMIN_TOKEN) can act for everyone — tools then take `user`.

Run it

* locally over stdio (Claude Desktop / Claude Code / any MCP client):
      MUSE_QUERY_URL=http://muse-host:8772 MUSE_TOKEN=muse_... muse-mcp
* or as a shared HTTP endpoint (streamable HTTP at /mcp), where each client sends its own
  `Authorization: Bearer <token>`:
      muse-mcp --http --host 0.0.0.0 --port 8003
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

QUERY_URL = os.environ.get("MUSE_QUERY_URL", "http://localhost:8772").rstrip("/")
ENV_TOKEN = os.environ.get("MUSE_TOKEN", "")

INSTRUCTIONS = """\
Muse is a self-hosted taste profile for a household: music (Mono, Navidrome), films and shows
(Jellyfin), YouTube, books, searches. It learns from what people play, finish, skip and rate,
and from what they tell it. These tools let you read that profile, search everything Muse
knows, get recommendations, and teach Muse what you learn in conversation.

Who: call `whoami` first. A personal token acts for one person; with a household token, call
`list_users`, make sure you know who you are talking to, and pass `user` explicitly. Never mix
one person's data into another's, and don't reveal one person's profile to someone else.

Knowing someone: read `get_taste_profile` before recommending or discussing taste. `search`
finds items across every source (semantic + keyword, ranked by the person's taste) and shows
what they already did with each. `recommend` gives Muse's picks with reasons; `taste_from`
uses one domain's taste for another (music taste -> films).

Asking: Muse keeps questions it wants answered (`get_questions`) — gaps in the profile, mixed
signals, uncertain picks. When the moment fits (they ask for better recommendations, are
chatting about taste, or invite it), ask ONE question at a time in your own words, then
record it with `answer_question` (an option value, or text). Don't interrogate; `skip_question`
when they don't want to answer. Questions you want answered later go to `ask_later`.

Remembering: when you learn a lasting preference ("can't stand musicals", "loves 90s Dutch
rock", "falls asleep to audiobooks"), call `remember` with a short, specific statement; set
`subject` when it names an artist, genre or title, and `kind=avoid` for never-suggest. Muse
folds it into recommendations. Briefly tell the person what you stored; `forget` on request.
Only store taste and listening/viewing context — no health, politics, religion, relationships
or other sensitive details, and nothing the person asks you to keep to yourself.

Feedback: `rate_item` records a thumbs (-1..2) when the person reacts to a specific item.
Ideas: if you notice Muse missing something that would have helped, `suggest_feature`.
Accounts: one person can have several ids (Jellyfin, Mono, ...); `create_link_code` on one
account and `link_account` on the other joins them into one profile. Only link accounts that
belong to the same person.
"""

mcp = MCPServer(name="muse", title="Muse", instructions=INSTRUCTIONS, version="0.1.0")
_transport: httpx.AsyncBaseTransport | None = None  # tests inject an ASGI transport

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _token(ctx: Context | None) -> str:
    """HTTP mode: the caller's own bearer token (verified by the query API). stdio: MUSE_TOKEN."""
    headers = None
    if ctx is not None:
        try:
            headers = ctx.headers
        except ValueError:
            headers = None
    if headers:
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
    if ENV_TOKEN:
        return ENV_TOKEN
    raise ToolError("No Muse token: set MUSE_TOKEN, or send Authorization: Bearer <token> to the HTTP endpoint.")


async def _api(ctx: Context | None, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=QUERY_URL, timeout=30, transport=_transport) as client:
        try:
            r = await client.request(method, path, params=clean, json=body,
                                     headers={"Authorization": f"Bearer {_token(ctx)}"})
        except httpx.HTTPError as e:
            raise ToolError(f"Muse query API unreachable at {QUERY_URL}: {e}") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        raise ToolError(f"Muse said {r.status_code}: {detail}")
    return r.json()


def _u(user: str | None) -> str:
    return user or "me"


# --- who ------------------------------------------------------------------------------------


@mcp.tool(annotations=READ)
async def whoami(ctx: Context) -> dict:
    """Who this connection acts for: one person (with their linked accounts), or the whole
    household (then pass `user` to other tools; ids are listed here)."""
    return await _api(ctx, "GET", "/v1/me")


@mcp.tool(annotations=READ)
async def list_users(ctx: Context) -> dict:
    """People Muse knows (household token), with their linked ids, activity per source and
    display names. A personal token only sees itself."""
    return await _api(ctx, "GET", "/v1/users")


@mcp.tool(annotations=READ)
async def capabilities(ctx: Context) -> dict:
    """Which sources/domains feed Muse, which known integrations aren't connected yet, and
    whether semantic search is on. Useful before suggesting features."""
    return await _api(ctx, "GET", "/v1/capabilities")


# --- knowing --------------------------------------------------------------------------------


@mcp.tool(annotations=READ)
async def get_taste_profile(ctx: Context, user: str = "me", domain: str | None = None, days: int = 365) -> dict:
    """A person's taste summary: per domain (track, movie, series, youtube_video, audiobook,
    ebook) the genres they love/dislike, top artists, favourites, least favourites; plus what
    they told Muse (facts), their settings, linked accounts, recent activity and how many
    questions Muse has for them. `domain` narrows it, `days` sets the window."""
    return await _api(ctx, "GET", f"/v1/users/{_u(user)}/profile", {"domain": domain, "days": days})


@mcp.tool(annotations=READ)
async def search(ctx: Context, query: str, item_type: str | None = None, source: str | None = None,
                 limit: int = 10, user: str = "me", personalize: bool = True) -> dict:
    """Search everything Muse has seen across all sources — by meaning ("melancholic Dutch
    songs", "slow-burn sci-fi") and by keyword (titles, artists). Results are ranked by the
    person's taste when `personalize`, and `you` shows what they already did with an item
    (played, skipped, rated...). item_type: track, movie, series, series_episode,
    youtube_video, audiobook, ebook. source: mono, jellyfin, ..."""
    return await _api(ctx, "GET", "/v1/search", {
        "q": query, "item_type": item_type, "source": source, "limit": limit, "user": user, "personalize": personalize,
    })


@mcp.tool(annotations=READ)
async def recommend(ctx: Context, item_type: str, limit: int = 10, user: str = "me", taste_from: str | None = None,
                    explore_ratio: float | None = None, source: str | None = None) -> dict:
    """Muse's recommendations for a person, with scores, confidence, an `explore` flag for
    deliberate wildcards, and human-readable `why`. `taste_from` ranks with another domain's
    taste (e.g. item_type=movie, taste_from=track: films for their music taste; `_combined`
    = everything). explore_ratio 0-1 overrides how adventurous the list is."""
    return await _api(ctx, "GET", f"/v1/users/{_u(user)}/recommendations", {
        "item_type": item_type, "limit": limit, "taste_from": taste_from, "explore_ratio": explore_ratio, "source": source,
    })


@mcp.tool(annotations=READ)
async def get_item(ctx: Context, source: str, source_item_id: str, user: str = "me") -> dict:
    """Details of one item (title, genres, description...), what the person did with it,
    and the most similar items."""
    return await _api(ctx, "GET", f"/v1/items/{source}/{source_item_id}", {"user": user})


@mcp.tool(annotations=READ)
async def similar_items(ctx: Context, source: str, source_item_id: str, item_type: str | None = None,
                        limit: int = 10, user: str = "me") -> dict:
    """Items closest in content to a given item ("more like this"); item_type can cross
    domains (a song -> films with a similar feel)."""
    return await _api(ctx, "GET", f"/v1/users/{_u(user)}/similar", {
        "source": source, "source_item_id": source_item_id, "item_type": item_type, "limit": limit,
    })


@mcp.tool(annotations=READ)
async def get_history(ctx: Context, user: str = "me", item_type: str | None = None, action: str | None = None,
                      limit: int = 30) -> dict:
    """Recent events, newest first. action: played, completed, skipped, favorited,
    unfavorited, rated, added_to_playlist, searched, rejected, ..."""
    return await _api(ctx, "GET", f"/v1/users/{_u(user)}/history", {"item_type": item_type, "action": action, "limit": limit})


# --- asking ---------------------------------------------------------------------------------


@mcp.tool(annotations=WRITE)
async def get_questions(ctx: Context, user: str = "me", limit: int = 3, domain: str | None = None) -> dict:
    """Questions Muse would like this person to answer, most useful first, each with `reason`
    (why Muse asks) and `options` (answer with an option value) or free text. Ask one at a
    time, in your own words. May create new questions when few are open."""
    return await _api(ctx, "GET", f"/v1/users/{_u(user)}/questions", {"limit": limit, "domain": domain})


@mcp.tool(annotations=WRITE)
async def answer_question(ctx: Context, question_id: str, option: str | None = None, text: str | None = None,
                          note: str | None = None) -> dict:
    """Record the person's answer: `option` = one of the question's option values (or its
    label), or `text` for free-text questions / when no option fits. `note`: anything extra
    they said that's worth keeping. Returns what Muse changed (a rating, a fact, a setting)."""
    return await _api(ctx, "POST", f"/v1/questions/{question_id}/answer", body={"option": option, "text": text, "note": note})


@mcp.tool(annotations=WRITE)
async def skip_question(ctx: Context, question_id: str, reason: str | None = None) -> dict:
    """The person doesn't want to answer (it won't be asked again)."""
    return await _api(ctx, "POST", f"/v1/questions/{question_id}/skip", body={"reason": reason})


@mcp.tool(annotations=WRITE)
async def ask_later(ctx: Context, prompt: str, kind: str = "free_text", options: list[str] | None = None,
                    domain: str = "general", reason: str | None = None, user: str = "me") -> dict:
    """Queue your own question for this person, to ask in a later conversation or show in an
    app (Mono shows Muse's questions). kind: free_text, yes_no, choice, scale (choice/scale
    need options)."""
    return await _api(ctx, "POST", f"/v1/users/{_u(user)}/questions", body={
        "prompt": prompt, "kind": kind, "options": options, "domain": domain, "reason": reason,
    })


# --- teaching -------------------------------------------------------------------------------


@mcp.tool(annotations=WRITE)
async def remember(ctx: Context, statement: str, kind: str = "likes", domain: str = "general",
                   subject: str | None = None, sentiment: float | None = None, confidence: float = 0.7,
                   user: str = "me") -> dict:
    """Tell Muse something you learned about the person's taste, so it shapes their
    recommendations. statement: short and specific ("Loves slow, sad Dutch ballads").
    kind: likes | dislikes | avoid (never suggest; needs subject) | context (when/how they
    watch or listen) | fact. domain: track, movie, series, youtube_video, audiobook, ebook or
    general. subject: the artist/genre/title it names. sentiment -1..1 (default from kind),
    confidence 0..1 (how sure you are)."""
    return await _api(ctx, "POST", f"/v1/users/{_u(user)}/facts", body={
        "statement": statement, "kind": kind, "domain": domain, "subject": subject,
        "sentiment": sentiment, "confidence": confidence,
    })


@mcp.tool(annotations=READ)
async def list_facts(ctx: Context, user: str = "me", domain: str | None = None) -> dict:
    """What Muse has been told about the person (by assistants, apps or their answers)."""
    return await _api(ctx, "GET", f"/v1/users/{_u(user)}/facts", {"domain": domain})


@mcp.tool(annotations=DESTRUCTIVE)
async def forget(ctx: Context, fact_id: str, user: str = "me") -> dict:
    """Retract a fact (wrong, outdated, or the person asked). Its effect on recommendations
    disappears on the next taste update."""
    return await _api(ctx, "DELETE", f"/v1/users/{_u(user)}/facts/{fact_id}")


@mcp.tool(annotations=WRITE)
async def rate_item(ctx: Context, source: str, source_item_id: str, rating: int, note: str | None = None,
                    user: str = "me") -> dict:
    """Explicit feedback on one item: -1 not for me, 0 clear, 1 good, 2 love it. Use when the
    person reacts to a specific film/song/video you discussed (find ids with `search`)."""
    return await _api(ctx, "POST", f"/v1/users/{_u(user)}/feedback", body={
        "source": source, "source_item_id": source_item_id, "rating": rating, "note": note,
    })


@mcp.tool(annotations=WRITE)
async def update_settings(ctx: Context, user: str = "me", explore_ratio: float | None = None,
                          taste_blend: float | None = None) -> dict:
    """Recommendation dials. explore_ratio 0-1: share of wildcards (default 0.125).
    taste_blend 0-1: how much the person's overall cross-domain taste (e.g. their music)
    shapes every other domain."""
    return await _api(ctx, "PUT", f"/v1/users/{_u(user)}/settings", body={"explore_ratio": explore_ratio, "taste_blend": taste_blend})


@mcp.tool(annotations=WRITE)
async def suggest_feature(ctx: Context, title: str, description: str, rationale: str | None = None) -> dict:
    """Propose an improvement to Muse itself (a missing source, a question it should ask, a
    control people want). Duplicates count as votes."""
    return await _api(ctx, "POST", "/v1/suggestions", body={"title": title, "description": description, "rationale": rationale})


@mcp.tool(annotations=READ)
async def list_feature_suggestions(ctx: Context, status: str | None = None) -> dict:
    """Ideas proposed for Muse so far, most votes first. status: new, planned, done, declined."""
    return await _api(ctx, "GET", "/v1/suggestions", {"status": status})


# --- accounts -------------------------------------------------------------------------------


@mcp.tool(annotations=READ)
async def list_identities(ctx: Context, user: str = "me") -> dict:
    """The accounts (ids per service) that make up this person's profile."""
    return await _api(ctx, "GET", f"/v1/users/{_u(user)}/identity")


@mcp.tool(annotations=WRITE)
async def create_link_code(ctx: Context, user: str = "me") -> dict:
    """A single-use code (15 minutes) to join another account of the SAME person to this
    profile: redeem it there (in Mono's settings, or an assistant connected as that account)."""
    return await _api(ctx, "POST", "/v1/identity/link-codes", {"user": user})


@mcp.tool(annotations=WRITE)
async def link_account(ctx: Context, code: str, label: str | None = None, user: str = "me") -> dict:
    """Redeem a link code made on another account of the same person: both become one
    profile (taste, facts and questions are shared from then on)."""
    return await _api(ctx, "POST", "/v1/identity/link", {"user": user}, body={"code": code, "label": label})


@mcp.tool(annotations=DESTRUCTIVE)
async def unlink_account(ctx: Context, alias_id: str) -> dict:
    """Split a linked account off again (ids from `list_identities`)."""
    return await _api(ctx, "DELETE", f"/v1/identity/aliases/{alias_id}")


# --- prompts --------------------------------------------------------------------------------


@mcp.prompt(title="Taste interview")
def taste_interview(user: str = "me", count: int = 5) -> str:
    """A short, friendly interview that fills the gaps in someone's Muse profile."""
    return (
        f"Help Muse get to know {'me' if user == 'me' else 'user ' + user} better. Read my taste profile "
        f"(get_taste_profile), then fetch up to {count} questions (get_questions). Ask them one at a time, "
        "conversationally, reacting to my answers; record each with answer_question. If I mention a lasting "
        "preference along the way, store it with remember and tell me in a few words. Finish with a short summary "
        "of what Muse learned and one recommendation that uses it."
    )


@mcp.prompt(title="Use my taste elsewhere")
def use_my_taste(from_domain: str = "track", to_domain: str = "movie", user: str = "me") -> str:
    """Recommendations in one domain driven by taste in another (e.g. films for your music taste)."""
    return (
        f"Using my taste in {from_domain} (get_taste_profile), find {to_domain} picks for me: call recommend with "
        f"item_type={to_domain} and taste_from={from_domain}, and try search with a few descriptive queries built from "
        "what I love. Give 5 picks, each with one line on how it connects to my taste. Ask whether Muse should keep "
        "using this cross-domain taste (update_settings taste_blend) based on my reaction."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Muse MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default=os.environ.get("MUSE_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MUSE_MCP_PORT", "8003")))
    args = parser.parse_args()
    if args.http:
        mcp.run("streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
