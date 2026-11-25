"""Music beyond the library: the wanted list (for a music-Radarr, Lidarr, webhooks) and new releases."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from muse_schema import notify
from pydantic import BaseModel, Field

from .auth import Principal, current_principal, principal_for_token, resolve_user, writer
from .db import get_pool

router = APIRouter(tags=["music"])
STATUSES = ("wanted", "grabbed", "in_library", "dismissed", "failed")


def wanted_json(r) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "user_id": str(r["user_id"]) if r["user_id"] else None, "kind": r["kind"],
        "mbid": r["mbid"], "artist_mbid": r["artist_mbid"], "artist": r["artist_name"], "title": r["title"],
        "release_date": r["release_date"].isoformat() if r["release_date"] else None, "reason": r["reason"],
        "score": round(float(r["score"]), 3), "origin": r["origin"], "status": r["status"], "external_id": r["external_id"],
        "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
    }


async def _scope(conn, p: Principal, user: str | None) -> uuid.UUID | None:
    """None = everyone's (admin without user); else one person (their rows + household rows)."""
    if p.admin and user in (None, "", "all"):
        return None
    return await resolve_user(conn, p, user or "me")


async def _list(conn, scope: uuid.UUID | None, status: str | None, kind: str | None, origin: str | None,
                min_score: float, limit: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT * FROM wanted
        WHERE ($1::uuid IS NULL OR user_id = $1 OR user_id IS NULL)
          AND ($2::text IS NULL OR status = $2) AND ($3::text IS NULL OR kind = $3) AND ($4::text IS NULL OR origin = $4)
          AND score >= $5
        ORDER BY score DESC, created_at DESC LIMIT $6
        """,
        scope, status, kind, origin, min_score, limit,
    )
    return [wanted_json(r) for r in rows]


@router.get("/v1/wanted")
async def wanted(
    user: str | None = Query(None, description="'me', a user id, or (admin) omit for the whole household"),
    status: str | None = Query("wanted", description=f"one of {STATUSES}, or omit for all"),
    kind: str | None = Query(None, description="release | recording | artist"),
    origin: str | None = Query(None, description="new_release | discovery | wishlist | assistant | user"),
    min_score: float = Query(0.0, ge=0, le=1),
    limit: int = Query(200, ge=1, le=1000),
    p: Principal = Depends(current_principal),
) -> dict:
    """What should be acquired. A music-Radarr polls this, grabs, and reports back with PUT."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        scope = await _scope(conn, p, user)
        return {"wanted": await _list(conn, scope, status, kind, origin, min_score, limit)}


@router.get("/v1/wanted/lidarr")
async def wanted_lidarr(token: str = Query(..., description="a Muse token (read-only is enough)"),
                        user: str | None = None, min_score: float = 0.5) -> list[dict]:
    """Import-list format for Lidarr-style tools: one entry per artist/album. Lidarr can only
    send a URL, hence the token in the query string — use a read-only token for this."""
    p = await principal_for_token(token)
    pool = await get_pool()
    async with pool.acquire() as conn:
        scope = await _scope(conn, p, user)
        rows = await _list(conn, scope, "wanted", None, None, min_score, 1000)
    out, seen = [], set()
    for w in rows:
        if not w["artist_mbid"]:
            continue
        key = (w["artist_mbid"], w["mbid"] if w["kind"] == "release" else None)
        if key in seen:
            continue
        seen.add(key)
        entry = {"MusicBrainzId": w["artist_mbid"], "ArtistMusicBrainzId": w["artist_mbid"], "ArtistName": w["artist"]}
        if w["kind"] == "release" and w["mbid"]:
            entry.update({"AlbumMusicBrainzId": w["mbid"], "AlbumTitle": w["title"]})
        out.append(entry)
    return out


class WantedIn(BaseModel):
    kind: str = Field("recording", description="release | recording | artist")
    artist: str
    title: str | None = None
    mbid: str | None = None
    artist_mbid: str | None = None
    reason: str | None = None
    score: float = Field(0.7, ge=0, le=1)


@router.post("/v1/users/{user}/wanted")
async def add_wanted(user: str, body: WantedIn, p: Principal = Depends(writer)) -> dict:
    if body.kind not in ("release", "recording", "artist"):
        raise HTTPException(422, "kind must be release, recording or artist")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        origin = "assistant" if p.kind == "user" else "user"
        key = f"manual:{body.kind}:{body.mbid or (body.artist + '|' + (body.title or '')).casefold()}"
        row = await conn.fetchrow(
            "INSERT INTO wanted (user_id, kind, mbid, artist_mbid, artist_name, title, reason, score, origin, dedupe_key) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (user_id, dedupe_key) DO UPDATE SET status = 'wanted', "
            "updated_at = now() RETURNING *",
            uid, body.kind, body.mbid, body.artist_mbid, body.artist, body.title, body.reason, body.score, origin, key,
        )
        await notify.emit(conn, "wanted", {"id": str(row["id"]), "kind": body.kind, "artist": body.artist, "title": body.title,
                                           "mbid": body.mbid, "artist_mbid": body.artist_mbid, "origin": origin}, user_id=uid)
        return wanted_json(row)


class WantedPatch(BaseModel):
    status: str
    external_id: str | None = None


@router.put("/v1/wanted/{wanted_id}")
async def update_wanted(wanted_id: str, body: WantedPatch, p: Principal = Depends(writer)) -> dict:
    """The acquiring tool reports: grabbed / in_library / failed. People can dismiss."""
    if body.status not in STATUSES:
        raise HTTPException(422, f"status must be one of {STATUSES}")
    try:
        wid = uuid.UUID(wanted_id)
    except ValueError:
        raise HTTPException(400, "wanted_id must be a UUID")
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchrow("SELECT user_id FROM wanted WHERE id = $1", wid)
        if owner is None:
            raise HTTPException(404, "no such wanted item")
        if not p.admin and p.kind != "adapter":
            mine = await resolve_user(conn, p, "me")
            if owner["user_id"] not in (None, mine):
                raise HTTPException(403, "that wanted item belongs to someone else")
        row = await conn.fetchrow(
            "UPDATE wanted SET status = $2, external_id = COALESCE($3, external_id), updated_at = now() WHERE id = $1 RETURNING *",
            wid, body.status, body.external_id,
        )
        return wanted_json(row)


@router.get("/v1/users/{user}/releases")
async def releases(user: str, days: int = Query(90, ge=1, le=730), include_upcoming: bool = True,
                   p: Principal = Depends(current_principal)) -> dict:
    """New (and upcoming) releases by artists this person loves."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        rows = await conn.fetch(
            """
            SELECT r.*, w.id AS wanted_id, w.status AS wanted_status, w.score
            FROM wanted w JOIN catalog_release r ON r.mbid = w.mbid
            WHERE w.user_id = $1 AND w.origin = 'new_release'
              AND (r.release_date IS NULL OR r.release_date >= current_date - $2::int)
              AND ($3 OR r.release_date IS NULL OR r.release_date <= current_date)
            ORDER BY r.release_date DESC NULLS LAST
            """,
            uid, days, include_upcoming,
        )
    today = date.today()
    return {"releases": [
        {"artist": r["artist_name"], "artist_mbid": r["artist_mbid"], "title": r["title"], "type": r["primary_type"],
         "release_group_mbid": r["mbid"], "release_date": r["release_date"].isoformat() if r["release_date"] else None,
         "upcoming": bool(r["release_date"] and r["release_date"] > today), "wanted_id": str(r["wanted_id"]),
         "status": r["wanted_status"], "affinity": round(float(r["score"]), 3)}
        for r in rows
    ]}


@router.get("/v1/catalog/artists")
async def catalog_artists(followed: bool = True, limit: int = Query(100, ge=1, le=1000),
                          p: Principal = Depends(current_principal)) -> dict:
    """Artists the catalog follows (household favourites) or knows (incl. discovered)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT mbid, name, country, tags, seed_weight, related, fetched_at FROM catalog_artist "
            "WHERE NOT $1 OR seed_weight > 0 ORDER BY seed_weight DESC, name LIMIT $2",
            followed, limit,
        )
    return {"artists": [
        {"mbid": r["mbid"], "name": r["name"], "country": r["country"], "tags": r["tags"], "followed": r["seed_weight"] > 0,
         "related": [{"name": x.get("name"), "mbid": x.get("mbid")} for x in (r["related"] or [])[:10]]}
        for r in rows
    ]}
