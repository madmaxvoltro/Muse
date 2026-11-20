"""Group sessions: who is watching or listening together, right now.

Any number can be active at once — A+B in the living room, C+K upstairs, a manual session in
Vesper — each with its own members, room and device. Sessions come from three places:
* an app (Vesper: "who's watching?") or any API client: POST /v1/groups;
* Home Assistant presence: POST /v1/integrations/home-assistant/presence per room; two or more
  people in a room = a session for that room, kept in sync as people come and go;
* an assistant (MCP), for "what should we watch, me and Sanne?".
Recommendations for a session come from the recommendation API (fairness, vetoes, least misery).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from muse_schema import canonical_user, notify
from pydantic import BaseModel, Field

from . import recclient
from .auth import Principal, admin_only, current_principal, resolve_user, writer
from .db import get_pool
from .situation import person_for

router = APIRouter(tags=["groups"])


def session_json(r) -> dict:
    return {"id": str(r["id"]), "name": r["name"], "room": r["room"], "device": r["device"], "members": [str(m) for m in r["members"]],
            "origin": r["origin"], "started_at": r["started_at"].isoformat(),
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None, "active": r["ended_at"] is None}


async def _members(conn, who: list[str]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for w in who:
        uid = await person_for(conn, w)
        if uid is None:
            raise HTTPException(422, f"unknown person {w!r} (a user id, or a Home Assistant person ref set with PUT /v1/users/{{u}}/refs/home_assistant)")
        c = await canonical_user(conn, uid)
        if c not in out:
            out.append(c)
    return out


async def _end_expired(conn) -> None:
    await conn.execute("UPDATE group_session SET ended_at = now() WHERE ended_at IS NULL AND expires_at < now()")


class GroupIn(BaseModel):
    members: list[str] = Field(..., min_length=1, description="user ids or Home Assistant person refs")
    name: str | None = None
    room: str | None = None
    device: str | None = None
    ttl_minutes: int = Field(240, ge=5, le=24 * 60)


@router.post("/v1/groups")
async def create_group(body: GroupIn, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        members = await _members(conn, body.members)
        if p.kind != "admin":
            me = await resolve_user(conn, p, "me")
            if me not in members:
                members.insert(0, me)  # you start a session you're part of
        row = await conn.fetchrow(
            "INSERT INTO group_session (name, room, device, members, origin, created_by, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *",
            body.name, body.room, body.device, members, p.source or ("assistant" if p.kind == "user" else "api"), p.label,
            datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes))
        await notify.emit(conn, "group", {"action": "started", **session_json(row)})
    return session_json(row)


@router.get("/v1/groups")
async def list_groups(active: bool = True, room: str | None = None, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _end_expired(conn)
        me = None if p.admin else await resolve_user(conn, p, "me")
        rows = await conn.fetch(
            "SELECT * FROM group_session WHERE (NOT $1 OR ended_at IS NULL) AND ($2::text IS NULL OR room = $2) "
            "AND ($3::uuid IS NULL OR $3 = ANY(members)) ORDER BY started_at DESC LIMIT 100", active, room, me)
    return {"groups": [session_json(r) for r in rows]}


class GroupPatch(BaseModel):
    add: list[str] = []
    remove: list[str] = []
    ttl_minutes: int | None = Field(None, ge=5, le=24 * 60)
    end: bool = False


async def _own_session(conn, p: Principal, gid: str):
    try:
        row = await conn.fetchrow("SELECT * FROM group_session WHERE id = $1", uuid.UUID(gid))
    except ValueError:
        raise HTTPException(400, "group id must be a UUID")
    if row is None:
        raise HTTPException(404, "no such group session")
    if not p.admin and await resolve_user(conn, p, "me") not in row["members"]:
        raise HTTPException(403, "you're not in that group")
    return row


@router.put("/v1/groups/{gid}")
async def update_group(gid: str, body: GroupPatch, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _own_session(conn, p, gid)
        members = list(row["members"])
        for m in await _members(conn, body.add):
            if m not in members:
                members.append(m)
        drop = set(await _members(conn, body.remove))
        members = [m for m in members if m not in drop]
        ended = body.end or not members
        row = await conn.fetchrow(
            "UPDATE group_session SET members = $2, expires_at = COALESCE($3, expires_at), ended_at = CASE WHEN $4 THEN now() ELSE ended_at END "
            "WHERE id = $1 RETURNING *", row["id"], members,
            datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes) if body.ttl_minutes else None, ended)
    return session_json(row)


@router.delete("/v1/groups/{gid}")
async def end_group(gid: str, p: Principal = Depends(writer)) -> dict:
    return await update_group(gid, GroupPatch(end=True), p)


@router.get("/v1/groups/{gid}/recommendations")
async def group_recs(gid: str, item_type: str = "movie", limit: int = Query(20, ge=1, le=100), seen: str = "all",
                     mood: str | None = None, source: list[str] | None = Query(None), p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _own_session(conn, p, gid)
    try:
        return await recclient.get("/group-recommendations", {"session": str(row["id"]), "item_type": item_type, "limit": limit,
                                                              "seen": seen, "mood": mood, "source": source, "room": row["room"]})
    except recclient.RecUnavailable as e:
        raise HTTPException(503, str(e))


@router.get("/v1/group-recommendations")
async def adhoc_group_recs(user: list[str] = Query(..., description="user ids or Home Assistant person refs"), item_type: str = "movie",
                           limit: int = Query(20, ge=1, le=100), seen: str = "all", mood: str | None = None,
                           p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        members = await _members(conn, user)
        if not p.admin and await resolve_user(conn, p, "me") not in members:
            raise HTTPException(403, "ask for groups you are part of")
    try:
        return await recclient.get("/group-recommendations", {"user_id": [str(m) for m in members], "item_type": item_type,
                                                              "limit": limit, "seen": seen, "mood": mood})
    except recclient.RecUnavailable as e:
        raise HTTPException(503, str(e))


class PickIn(BaseModel):
    source: str
    source_item_id: str
    item_type: str = "movie"


@router.post("/v1/groups/{gid}/picks")
async def pick(gid: str, body: PickIn, p: Principal = Depends(writer)) -> dict:
    """What the group chose. Muse remembers how well it fitted each member, so whoever
    compromised gets more say next time."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _own_session(conn, p, gid)
    try:
        fits = (await recclient.get(f"/fit/{body.source}/{body.source_item_id}",
                                    {"user_id": [str(m) for m in row["members"]], "item_type": body.item_type}))["fits"]
    except recclient.RecUnavailable:
        fits = {}
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO group_pick (session_id, members, source, source_item_id, item_type, fits) VALUES ($1,$2,$3,$4,$5,$6)",
                           row["id"], row["members"], body.source, body.source_item_id, body.item_type, fits)
    return {"session": gid, "fits": fits}


# --- Home Assistant -------------------------------------------------------------------------------------


class PresenceIn(BaseModel):
    room: str
    people: list[str] = Field(..., description="who is in the room now: Home Assistant person refs or user ids (empty = nobody)")
    ttl_minutes: int = Field(240, ge=5, le=24 * 60)


@router.post("/v1/integrations/home-assistant/presence")
async def ha_presence(body: PresenceIn, p: Principal = Depends(admin_only)) -> dict:
    """Who is in a room. Two or more people = a group session for that room (created, updated,
    or ended as people come and go). Unknown refs are reported, not fatal."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        known, unknown = [], []
        for w in body.people:
            uid = await person_for(conn, w)
            if uid is None:
                unknown.append(w)
            else:
                c = await canonical_user(conn, uid)
                if c not in known:
                    known.append(c)
        expires = datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)
        async with conn.transaction():
            await conn.execute("DELETE FROM room_presence WHERE room = $1", body.room)
            for uid in known:
                await conn.execute("INSERT INTO room_presence (room, user_id, expires_at) VALUES ($1, $2, $3)", body.room, uid, expires)
            current = await conn.fetchrow(
                "SELECT * FROM group_session WHERE room = $1 AND origin = 'home_assistant' AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
                body.room)
            session = None
            if len(known) >= 2:
                if current is not None and set(current["members"]) == set(known):
                    session = await conn.fetchrow("UPDATE group_session SET expires_at = $2 WHERE id = $1 RETURNING *", current["id"], expires)
                else:
                    if current is not None:
                        await conn.execute("UPDATE group_session SET ended_at = now() WHERE id = $1", current["id"])
                    session = await conn.fetchrow(
                        "INSERT INTO group_session (name, room, members, origin, created_by, expires_at) "
                        "VALUES ($1, $1, $2, 'home_assistant', 'home_assistant', $3) RETURNING *", body.room, known, expires)
                    await notify.emit(conn, "group", {"action": "started", **session_json(session)})
            elif current is not None:
                await conn.execute("UPDATE group_session SET ended_at = now() WHERE id = $1", current["id"])
    return {"room": body.room, "present": [str(k) for k in known], "unknown": unknown,
            "group": session_json(session) if session else None}


@router.get("/v1/rooms")
async def rooms(p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT room, array_agg(user_id) AS people FROM room_presence WHERE expires_at IS NULL OR expires_at > now() GROUP BY room")
    return {"rooms": [{"room": r["room"], "people": [str(x) for x in r["people"]]} for r in rows]}
