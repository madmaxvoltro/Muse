"""The admin's side of Muse in a client: the notification feed, the curator's controls and
QR pairing.

* Feed — what the curator did (added, Soon Gone, removed, saved, next season, disk low) and,
  for music, new releases and the household wanted list. Vesper shows `domain=video`, Mono
  `domain=music`; `system` entries show in both. Admin only.
* Curator — status (budget, usage, disk, what's on Soon Gone, recent activity) and the
  switches: keep (pin) a title, remove it now, pause, set the budget, run a cycle now.
* Pairing — `./muse pair` (or the web setup) asks for a one-time code with the client
  configuration; the QR on screen carries the code; the phone redeems it once at
  `POST /v1/pair` and gets the URLs and keys, so nobody types a key.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from muse_schema.crypto import P_ADMIN_NOTE, P_CURATOR, P_PAIRING, get_vault
from pydantic import BaseModel, Field

from . import identity
from .auth import Principal, admin_only
from .db import get_pool

router = APIRouter(tags=["admin"])

ARR_TYPE = {"movie": "movie", "series": "series_episode", "series_episode": "series_episode"}
PUBLIC_TYPE = {"movie": "movie", "series_episode": "series"}


def _arr_type(item_type: str) -> str:
    if item_type not in ARR_TYPE:
        raise HTTPException(400, "item_type must be movie or series")
    return ARR_TYPE[item_type]


def _uuid(v: str) -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except ValueError:
        raise HTTPException(404, "not found")


def _open(v, purpose):
    return (get_vault().open_json(v, purpose) or {}) if v else {}


def _note(r) -> dict:
    data = _open(r["data"], P_ADMIN_NOTE)
    return {
        "id": str(r["id"]),
        "created_at": r["created_at"].isoformat(),
        "domain": r["domain"],
        "kind": r["kind"],
        "severity": r["severity"],
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "actions": [] if r["resolved_at"] else data.get("actions", []),
        "item": {"type": PUBLIC_TYPE.get(r["item_type"], r["item_type"]), "tmdb_id": r["source_item_id"]} if r["source_item_id"] else None,
        "read": r["read_at"] is not None,
        "resolved": r["resolved_at"] is not None,
    }


# --- feed ------------------------------------------------------------------------------------------

@router.get("/v1/admin/notifications")
async def notifications(
    domain: Literal["video", "music", "all"] = "all",
    unread: bool = False,
    limit: int = Query(50, ge=1, le=200),
    before: datetime | None = None,
    p: Principal = Depends(admin_only),
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM admin_notification
               WHERE ($1 = 'all' OR domain = $1 OR domain = 'system') AND (NOT $2 OR read_at IS NULL)
                 AND ($3::timestamptz IS NULL OR created_at < $3)
               ORDER BY created_at DESC LIMIT $4""",
            domain, unread, before, limit,
        )
        count = await conn.fetchval(
            "SELECT count(*) FROM admin_notification WHERE read_at IS NULL AND ($1 = 'all' OR domain = $1 OR domain = 'system')", domain,
        )
    return {"unread": count, "notifications": [_note(r) for r in rows]}


@router.post("/v1/admin/notifications/{note_id}/read")
async def mark_read(note_id: str, p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.execute("UPDATE admin_notification SET read_at = coalesce(read_at, now()) WHERE id = $1", _uuid(note_id))
    if n.endswith(" 0"):
        raise HTTPException(404, "no such notification")
    return {"read": note_id}


@router.post("/v1/admin/notifications/read-all")
async def mark_all_read(domain: Literal["video", "music", "all"] = "all", p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.execute(
            "UPDATE admin_notification SET read_at = now() WHERE read_at IS NULL AND ($1 = 'all' OR domain = $1 OR domain = 'system')", domain,
        )
    return {"marked": int(n.split()[-1])}


@router.post("/v1/admin/notifications/{note_id}/actions/{action}")
async def act(note_id: str, action: Literal["keep", "remove_now"], p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow("SELECT * FROM admin_notification WHERE id = $1", _uuid(note_id))
        if r is None:
            raise HTTPException(404, "no such notification")
        if action not in _open(r["data"], P_ADMIN_NOTE).get("actions", []) or not r["source_item_id"]:
            raise HTTPException(400, f"'{action}' isn't offered on this notification")
        if r["resolved_at"]:
            raise HTTPException(409, "already handled")
        async with conn.transaction():
            result = await (_keep if action == "keep" else _remove_now)(conn, r["item_type"], r["source_item_id"], p)
            await conn.execute("UPDATE admin_notification SET resolved_at = now(), read_at = coalesce(read_at, now()) WHERE id = $1", r["id"])
            # the same title may have several entries (added, then Soon Gone): handle them all
            await conn.execute(
                "UPDATE admin_notification SET resolved_at = now() WHERE resolved_at IS NULL AND item_type = $1 AND source_item_id = $2 AND kind LIKE 'curator.%'",
                r["item_type"], r["source_item_id"],
            )
    return result


# --- curator ---------------------------------------------------------------------------------------

async def _managed(conn, item_type: str, tmdb_id: str):
    row = await conn.fetchrow(
        "SELECT * FROM curator_managed_item WHERE item_type = $1 AND source_item_id = $2 AND removed_at IS NULL", item_type, tmdb_id,
    )
    if row is None:
        raise HTTPException(404, "the curator doesn't manage this title (only titles it added or that carry its tag in Radarr/Sonarr)")
    return row


async def _activity(conn, kind: str, item_type: str, tmdb_id: str, data: dict) -> None:
    await conn.execute(
        "INSERT INTO curator_activity (kind, item_type, source_item_id, data) VALUES ($1, $2, $3, $4)",
        kind, item_type, tmdb_id, get_vault().seal_json(data, P_CURATOR),
    )


async def _keep(conn, item_type: str, tmdb_id: str, p: Principal) -> dict:
    row = await _managed(conn, item_type, tmdb_id)
    await conn.execute("UPDATE curator_managed_item SET pinned = TRUE, pinned_by = $2 WHERE id = $1", row["id"], p.label)
    saved = await conn.execute(
        "UPDATE soon_gone SET whitelisted = TRUE, forced = FALSE WHERE item_type = $1 AND source_item_id = $2 AND whitelisted = FALSE AND removed = FALSE",
        item_type, tmdb_id,
    )
    await _activity(conn, "kept", item_type, tmdb_id, {**_open(row["data"], P_CURATOR), "by": "admin"})
    return {"kept": True, "was_on_soon_gone": not saved.endswith(" 0")}


async def _remove_now(conn, item_type: str, tmdb_id: str, p: Principal) -> dict:
    row = await _managed(conn, item_type, tmdb_id)
    await conn.execute("UPDATE curator_managed_item SET pinned = FALSE WHERE id = $1", row["id"])
    n = await conn.execute(
        "UPDATE soon_gone SET expires_at = now(), forced = TRUE WHERE item_type = $1 AND source_item_id = $2 AND whitelisted = FALSE AND removed = FALSE",
        item_type, tmdb_id,
    )
    if n.endswith(" 0"):
        await conn.execute(
            "INSERT INTO soon_gone (source_item_id, item_type, arr_id, expires_at, forced) VALUES ($1, $2, $3, now(), TRUE)",
            tmdb_id, item_type, row["arr_id"],
        )
    await conn.execute("UPDATE curator_setting SET run_requested_at = now() WHERE id")  # the curator picks it up within a minute
    await _activity(conn, "remove_requested", item_type, tmdb_id, {**_open(row["data"], P_CURATOR), "by": "admin"})
    return {"removing": True, "note": "the curator removes it through Radarr/Sonarr within a minute or two"}


@router.post("/v1/admin/curator/items/{item_type}/{tmdb_id}/keep")
async def keep(item_type: str, tmdb_id: str, p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        return await _keep(conn, _arr_type(item_type), tmdb_id, p)


@router.delete("/v1/admin/curator/items/{item_type}/{tmdb_id}/keep")
async def unkeep(item_type: str, tmdb_id: str, p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await _managed(conn, _arr_type(item_type), tmdb_id)
        await conn.execute("UPDATE curator_managed_item SET pinned = FALSE, pinned_by = NULL WHERE id = $1", row["id"])
    return {"kept": False}


@router.post("/v1/admin/curator/items/{item_type}/{tmdb_id}/remove-now")
async def remove_now(item_type: str, tmdb_id: str, p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        return await _remove_now(conn, _arr_type(item_type), tmdb_id, p)


class CuratorSettings(BaseModel):
    paused: bool | None = None
    budget_gb: int | None = Field(None, ge=10, le=100000)


@router.patch("/v1/admin/curator")
async def curator_settings(body: CuratorSettings, p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE curator_setting SET paused = coalesce($1, paused), budget_gb = coalesce($2, budget_gb), updated_at = now() WHERE id",
            body.paused, body.budget_gb,
        )
    return await curator_status(activity=30, p=p)


@router.post("/v1/admin/curator/run")
async def curator_run(p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE curator_setting SET run_requested_at = now() WHERE id")
    return {"requested": True, "note": "the curator starts a full cycle within a minute"}


@router.get("/v1/admin/curator")
async def curator_status(activity: int = Query(30, ge=0, le=200), p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        s = await conn.fetchrow("SELECT * FROM curator_setting WHERE id")
        items = await conn.fetch("SELECT * FROM curator_managed_item WHERE removed_at IS NULL ORDER BY added_at DESC")
        pending = await conn.fetch(
            "SELECT source_item_id, item_type, expires_at, forced FROM soon_gone WHERE whitelisted = FALSE AND removed = FALSE ORDER BY expires_at"
        )
        acts = await conn.fetch("SELECT * FROM curator_activity ORDER BY at DESC LIMIT $1", activity)
    last = (s["last_status"] if s else None) or {}
    budget_gb = (s["budget_gb"] if s and s["budget_gb"] else None) or int(last.get("budget_bytes", 300 * 1024**3) / 1024**3)

    def size(r) -> int:
        return int(r["size_bytes"] or r["estimated_bytes"] or 0)

    def item(r) -> dict:
        d = _open(r["data"], P_CURATOR)
        return {"type": PUBLIC_TYPE.get(r["item_type"], r["item_type"]), "tmdb_id": r["source_item_id"], "title": d.get("title"), "year": d.get("year"),
                "size_bytes": size(r), "size_is_estimate": not r["size_bytes"], "added_at": r["added_at"].isoformat(), "pinned": r["pinned"],
                "adopted": r["adopted"], "seasons": {"monitored": r["seasons_monitored"], "total": r["seasons_total"]} if r["item_type"] != "movie" else None,
                "reason": d.get("reasons") or d.get("reason")}

    by_key = {(r["item_type"], r["source_item_id"]): r for r in items}
    return {
        "paused": bool(s and s["paused"]),
        "budget_bytes": budget_gb * 1024**3,
        "usage_bytes": sum(size(r) for r in items),
        "free_bytes": last.get("free_bytes"),
        "last_cycle_at": s["last_cycle_at"].isoformat() if s and s["last_cycle_at"] else None,
        "run_requested": bool(s and s["run_requested_at"]),
        "errors": last.get("errors", []),
        "counts": {"managed": len(items), "pinned": sum(1 for r in items if r["pinned"]), "soon_gone": len(pending)},
        "soon_gone": [
            {**(item(by_key[(r["item_type"], r["source_item_id"])]) if (r["item_type"], r["source_item_id"]) in by_key else
                {"type": PUBLIC_TYPE.get(r["item_type"], r["item_type"]), "tmdb_id": r["source_item_id"]}),
             "expires_at": r["expires_at"].isoformat(), "removing_now": r["forced"]}
            for r in pending
        ],
        "items": [item(r) for r in items],
        "activity": [
            {"at": a["at"].isoformat(), "kind": a["kind"], "type": PUBLIC_TYPE.get(a["item_type"], a["item_type"]), "tmdb_id": a["source_item_id"],
             **{k: v for k, v in _open(a["data"], P_CURATOR).items() if k in ("title", "year", "reason", "reasons", "season", "by", "score")}}
            for a in acts
        ],
    }


# --- pairing ---------------------------------------------------------------------------------------

def _code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


class PairingIn(BaseModel):
    purpose: Literal["vesper", "mono", "admin"] = "vesper"
    payload: dict = Field(default_factory=dict, description="what the client receives: URLs, keys, CA certificate (built by ./muse)")
    ttl_minutes: int = Field(15, ge=1, le=60 * 24 * 7)
    admin_token: bool = Field(False, description="also mint a household token, so the client can show the admin feed")
    user: str | None = Field(None, description="bind a personal token to this user (an invite for one person)")


@router.post("/v1/admin/pairing")
async def new_pairing(body: PairingIn, p: Principal = Depends(admin_only)) -> dict:
    code = secrets.token_urlsafe(18)
    payload = dict(body.payload)
    payload["purpose"] = body.purpose
    if body.admin_token:
        payload["_mint"] = {"kind": "admin"}
    elif body.user:
        payload["_mint"] = {"kind": "user", "user": body.user}
    expires = datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pairing_code WHERE expires_at < now() - interval '1 day'")
        await conn.execute(
            "INSERT INTO pairing_code (code_hash, purpose, payload, expires_at) VALUES ($1, $2, $3, $4)",
            _code_hash(code), body.purpose, get_vault().seal_json(payload, P_PAIRING), expires,
        )
    return {"code": code, "expires_at": expires.isoformat()}


class PairIn(BaseModel):
    code: str = Field(..., min_length=10, max_length=200)
    device: str | None = Field(None, max_length=80, description="shown in the token list, e.g. 'Pixel 9'")


@router.post("/v1/pair")
async def pair(body: PairIn) -> dict:
    """Redeem a pairing code once. No authentication: the code is the secret (144 bits, short-lived, single use)."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE pairing_code SET used_at = now() WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now() RETURNING purpose, payload",
            _code_hash(body.code),
        )
        if row is None:
            raise HTTPException(404, "this code is unknown, used or expired: make a new one with ./muse pair")
        payload = _open(row["payload"], P_PAIRING)
        mint = payload.pop("_mint", None)
        label = f"{row['purpose']} pairing" + (f" ({body.device})" if body.device else "")
        if mint and mint["kind"] == "admin":
            payload["admin_token"] = (await identity.create_token(conn, None, label, True))["token"]
        elif mint and mint["kind"] == "user":
            payload["user_token"] = (await identity.create_token(conn, _uuid(mint["user"]), label, True))["token"]
            payload["user_id"] = mint["user"]
    return payload
