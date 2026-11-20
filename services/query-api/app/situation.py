"""Context: what's going on right now, from apps, assistants, Home Assistant, calendars.

Signals are short-lived facts with an expiry ("mood: focus for 2 hours", "living room: TV on",
"activity: cooking"). The recommendation API reads the active ones on every request; nothing
here changes the long-term taste profile.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from muse_schema import moods as M
from muse_schema import notify
from pydantic import BaseModel, Field

from .auth import Principal, admin_only, current_principal, resolve_user, writer
from .db import get_pool
from .vectors import sound_vector, text_vector

router = APIRouter(tags=["context"])

KINDS = ("mood", "activity", "presence", "device", "weather", "calendar", "location", "custom")
DEFAULT_TTL = {"mood": 180, "activity": 120, "device": 30, "presence": 240, "weather": 90, "calendar": 60, "location": 120, "custom": 60}
ACTIVITIES = ("cooking", "working", "studying", "working_out", "running", "sleeping", "going_to_sleep", "driving", "commuting",
              "party", "dinner", "relaxing", "reading", "tv", "gaming")


class SignalIn(BaseModel):
    kind: str = Field(..., description=f"one of {KINDS}")
    value: dict[str, Any] = Field(..., description='e.g. {"mood": "focus"}, {"activity": "cooking"}, {"device": "Living room TV"}')
    ttl_minutes: int | None = Field(None, ge=1, le=7 * 24 * 60, description="how long it holds (defaults per kind)")
    room: str | None = None


def signal_json(r) -> dict:
    v = dict(r["value"] or {})
    v.pop("vec_text", None)
    v.pop("vec_audio", None)
    return {"id": str(r["id"]), "kind": r["kind"], "value": v, "room": r["room"], "source": r["source"],
            "user_id": str(r["user_id"]) if r["user_id"] else None, "created_at": r["created_at"].isoformat(),
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None}


async def _prepare(body: SignalIn) -> dict:
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {KINDS}")
    value = dict(body.value)
    if body.kind == "mood":
        mood = str(value.get("mood") or value.get("text") or "").strip()
        if not mood or len(mood) > 200:
            raise HTTPException(422, 'a mood needs {"mood": "<preset or a few words>"} (max 200 characters)')
        name, d = M.resolve(mood)
        value = {"mood": name or mood, "preset": name is not None}
        if name is None:  # a custom mood: embed it now so recommendations can use it right away
            value["vec_text"] = await text_vector(d["text"])
            value["vec_audio"] = await sound_vector(d["sound"])
    if body.kind == "activity" and value.get("activity") not in ACTIVITIES:
        raise HTTPException(422, f"activity must be one of {ACTIVITIES}")
    return value


async def _insert(conn, user_id, room, body: SignalIn, value: dict, source: str) -> dict:
    ttl = body.ttl_minutes or DEFAULT_TTL[body.kind]
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    # One active signal per (who, room, kind): a new mood replaces the old one.
    await conn.execute(
        "UPDATE context_signal SET expires_at = now() WHERE kind = $1 AND user_id IS NOT DISTINCT FROM $2 "
        "AND room IS NOT DISTINCT FROM $3 AND (expires_at IS NULL OR expires_at > now())", body.kind, user_id, room)
    row = await conn.fetchrow(
        "INSERT INTO context_signal (user_id, room, kind, value, source, expires_at) VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
        user_id, room, body.kind, value, source, expires)
    await notify.emit(conn, "context", {"kind": body.kind, "room": room, "value": {k: v for k, v in value.items() if not k.startswith("vec_")}},
                      user_id=user_id)
    return signal_json(row)


def _source(p: Principal) -> str:
    return p.source or ("assistant" if p.kind == "user" else p.kind)


@router.get("/v1/moods")
async def moods(p: Principal = Depends(current_principal)) -> dict:
    return {"presets": [{"name": k, "description": v["text"], "explore": v.get("explore")} for k, v in M.MOODS.items()],
            "custom": "any short text works too, e.g. 'rainy sunday, cooking for friends'"}


@router.post("/v1/users/{user}/context")
async def set_context(user: str, body: SignalIn, p: Principal = Depends(writer)) -> dict:
    """Tell Muse what's going on for this person (mood, activity, device, ...), for a while."""
    value = await _prepare(body)
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        return await _insert(conn, uid, body.room, body, value, _source(p))


@router.get("/v1/users/{user}/context")
async def get_context(user: str, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        rows = await conn.fetch(
            "SELECT * FROM context_signal WHERE (user_id = $1 OR user_id IS NULL) AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY created_at DESC", uid)
        presence = await conn.fetch("SELECT room FROM room_presence WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > now())", uid)
    return {"signals": [signal_json(r) for r in rows], "rooms": [r["room"] for r in presence]}


@router.delete("/v1/users/{user}/context/{kind}")
async def clear_context(user: str, kind: str, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        res = await conn.execute(
            "UPDATE context_signal SET expires_at = now() WHERE user_id = $1 AND kind = $2 AND (expires_at IS NULL OR expires_at > now())", uid, kind)
    return {"cleared": int(res.split()[-1])}


@router.post("/v1/rooms/{room}/context")
async def room_context(room: str, body: SignalIn, p: Principal = Depends(admin_only)) -> dict:
    """Room-level signal (Home Assistant: 'living room: tv on', 'kitchen: cooking')."""
    value = await _prepare(body)
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _insert(conn, None, room, body, value, _source(p))


@router.post("/v1/household/context")
async def household_context(body: SignalIn, p: Principal = Depends(admin_only)) -> dict:
    """Household-wide signal (weather pushed by Home Assistant, a party at home...)."""
    value = await _prepare(body)
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _insert(conn, None, None, body, value, _source(p))


# --- people in other systems ------------------------------------------------------------------------


class RefIn(BaseModel):
    ref: str = Field(..., description="e.g. 'person.max' (Home Assistant person entity)")


@router.put("/v1/users/{user}/refs/{system}")
async def set_ref(user: str, system: str, body: RefIn, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        await conn.execute("INSERT INTO person_ref (system, ref, user_id) VALUES ($1, $2, $3) "
                           "ON CONFLICT (system, ref) DO UPDATE SET user_id = $3", system, body.ref.strip(), uid)
    return {"system": system, "ref": body.ref.strip(), "user_id": str(uid)}


async def person_for(conn, who: str) -> uuid.UUID | None:
    """A Muse user id, or a Home Assistant person ref ('person.max')."""
    try:
        return uuid.UUID(who)
    except ValueError:
        return await conn.fetchval("SELECT user_id FROM person_ref WHERE system = 'home_assistant' AND ref = $1", who)


# --- calendars -------------------------------------------------------------------------------------------


class CalendarIn(BaseModel):
    url: str = Field(..., description="an ICS/webcal URL (Google 'secret address in iCal format', Nextcloud, Outlook ...)")
    label: str | None = None


@router.post("/v1/users/{user}/calendars")
async def add_calendar(user: str, body: CalendarIn, p: Principal = Depends(writer)) -> dict:
    url = body.url.strip().replace("webcal://", "https://", 1)
    if not url.startswith(("https://", "http://")):
        raise HTTPException(422, "calendar url must be http(s) or webcal")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        row = await conn.fetchrow("INSERT INTO calendar_feed (user_id, url, label) VALUES ($1, $2, $3) RETURNING id", uid, url, body.label)
    return {"id": str(row["id"]), "label": body.label,
            "note": "Muse reads it every 15 minutes and keeps only what kind of moment it is (workout, work, holiday...), never titles."}


@router.get("/v1/users/{user}/calendars")
async def calendars(user: str, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        rows = await conn.fetch("SELECT id, label, last_fetched_at, last_error FROM calendar_feed WHERE user_id = $1", uid)
    return {"calendars": [{"id": str(r["id"]), "label": r["label"], "last_fetched_at": r["last_fetched_at"].isoformat() if r["last_fetched_at"] else None,
                           "last_error": r["last_error"]} for r in rows]}


@router.delete("/v1/users/{user}/calendars/{cal_id}")
async def remove_calendar(user: str, cal_id: str, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        res = await conn.execute("DELETE FROM calendar_feed WHERE id = $1 AND user_id = $2", uuid.UUID(cal_id), uid)
    return {"deleted": int(res.split()[-1])}
