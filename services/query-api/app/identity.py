"""People, linked identities, link codes and API tokens."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from muse_schema import canonical_user, identity_group

from .auth import hash_secret

LINK_CODE_TTL = timedelta(minutes=15)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


async def ensure_user(conn, user_id: uuid.UUID, display_name: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO muse_user (user_id, display_name) VALUES ($1, $2) "
        "ON CONFLICT (user_id) DO UPDATE SET display_name = COALESCE(EXCLUDED.display_name, muse_user.display_name)",
        user_id, display_name,
    )


async def mark_dirty(conn, user_id: uuid.UUID) -> None:
    await conn.execute("INSERT INTO taste_dirty (user_id) VALUES ($1) ON CONFLICT (user_id) DO UPDATE SET marked_at = now()", user_id)


async def create_link_code(conn, user_id: uuid.UUID) -> dict:
    canonical = await canonical_user(conn, user_id)
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    expires = datetime.now(timezone.utc) + LINK_CODE_TTL
    await conn.execute(
        "INSERT INTO link_code (code_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        hash_secret(_normalize_code(code)), canonical, expires,
    )
    return {
        "code": code,
        "expires_at": expires.isoformat(),
        "how": "Redeem it from the other account (another app, or an assistant connected as that account) "
               "within 15 minutes. Both accounts then share one taste profile.",
    }


async def _source_of(conn, uid: uuid.UUID) -> str | None:
    return await conn.fetchval(
        "SELECT source FROM event WHERE user_id = $1 AND source <> 'muse' GROUP BY source ORDER BY count(*) DESC LIMIT 1", uid
    )


async def merge(conn, keep: uuid.UUID, other: uuid.UUID, linked_by: str, label: str | None = None) -> dict:
    """Make `other`'s whole group part of `keep`'s group. Both may be aliases; canonicals are used.
    Facts, questions and overlay of the absorbed person move to the kept canonical id."""
    keep_c = await canonical_user(conn, keep)
    other_c, other_group = await identity_group(conn, other)
    if keep_c == other_c:
        return {"user_id": str(keep_c), "linked": [], "note": "already linked"}
    async with conn.transaction():
        await conn.execute("UPDATE user_alias SET user_id = $1 WHERE user_id = $2", keep_c, other_c)
        await conn.execute(
            "INSERT INTO user_alias (alias_id, user_id, source, label, linked_by) VALUES ($1, $2, $3, $4, $5)",
            other_c, keep_c, await _source_of(conn, other_c), label, linked_by,
        )
        await conn.execute("UPDATE profile_fact SET user_id = $1 WHERE user_id = $2", keep_c, other_c)
        # Questions: keep the kept person's copy when both have the same generator key.
        await conn.execute(
            "DELETE FROM question q WHERE q.user_id = $2 AND q.dedupe_key IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM question k WHERE k.user_id = $1 AND k.dedupe_key = q.dedupe_key)",
            keep_c, other_c,
        )
        await conn.execute("UPDATE question SET user_id = $1 WHERE user_id = $2", keep_c, other_c)
        # Sliders: the kept person's win; the absorbed person's move over only if there are none.
        if await conn.fetchval("SELECT 1 FROM user_overlay WHERE user_id = $1", keep_c):
            await conn.execute("DELETE FROM user_overlay WHERE user_id = $1", other_c)
        else:
            await conn.execute("UPDATE user_overlay SET user_id = $1 WHERE user_id = $2", keep_c, other_c)
        await ensure_user(conn, keep_c)
        name = await conn.fetchval("SELECT display_name FROM muse_user WHERE user_id = $1", keep_c)
        if not name:
            other_name = await conn.fetchval("SELECT display_name FROM muse_user WHERE user_id = $1", other_c)
            if other_name:
                await conn.execute("UPDATE muse_user SET display_name = $2 WHERE user_id = $1", keep_c, other_name)
        await mark_dirty(conn, keep_c)
    return {"user_id": str(keep_c), "linked": [str(u) for u in other_group]}


async def redeem_link_code(conn, redeemer: uuid.UUID, code: str, label: str | None) -> dict:
    row = await conn.fetchrow(
        "UPDATE link_code SET used_at = now(), used_by = $2 WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now() "
        "RETURNING user_id",
        hash_secret(_normalize_code(code)), redeemer,
    )
    if row is None:
        raise HTTPException(400, "unknown, used or expired link code")
    return await merge(conn, row["user_id"], redeemer, "link_code", label)


async def unlink(conn, alias_id: uuid.UUID) -> dict:
    canonical = await conn.fetchval("DELETE FROM user_alias WHERE alias_id = $1 RETURNING user_id", alias_id)
    if canonical is None:
        raise HTTPException(404, "not a linked identity")
    await mark_dirty(conn, canonical)
    await mark_dirty(conn, alias_id)
    return {"unlinked": str(alias_id), "from": str(canonical),
            "note": "Facts and answers given while linked stay with the remaining profile."}


async def describe_identity(conn, canonical: uuid.UUID) -> dict:
    _, group = await identity_group(conn, canonical)
    rows = await conn.fetch(
        "SELECT user_id, source, count(*) AS n, max(\"timestamp\") AS last FROM event "
        "WHERE user_id = ANY($1::uuid[]) GROUP BY 1, 2",
        group,
    )
    aliases = {r["alias_id"]: r for r in await conn.fetch("SELECT * FROM user_alias WHERE user_id = $1", canonical)}
    ids = []
    for uid in group:
        srcs = {r["source"]: r["n"] for r in rows if r["user_id"] == uid}
        a = aliases.get(uid)
        ids.append({
            "id": str(uid),
            "canonical": uid == canonical,
            "label": a["label"] if a else None,
            "events_by_source": srcs,
            "linked_at": a["created_at"].isoformat() if a else None,
        })
    name = await conn.fetchval("SELECT display_name FROM muse_user WHERE user_id = $1", canonical)
    return {"user_id": str(canonical), "display_name": name, "identities": ids}


async def list_people(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        WITH ids AS (
            SELECT user_id, source, count(*) AS n, max("timestamp") AS last FROM event GROUP BY 1, 2
        )
        SELECT COALESCE(a.user_id, ids.user_id) AS canonical, ids.user_id, ids.source, ids.n, ids.last
        FROM ids LEFT JOIN user_alias a ON a.alias_id = ids.user_id
        """
    )
    people: dict[uuid.UUID, dict] = {}
    for r in rows:
        p = people.setdefault(r["canonical"], {"user_id": str(r["canonical"]), "ids": set(), "events_by_source": {}, "last_active": None})
        p["ids"].add(str(r["user_id"]))
        p["events_by_source"][r["source"]] = p["events_by_source"].get(r["source"], 0) + r["n"]
        if p["last_active"] is None or r["last"] > p["last_active"]:
            p["last_active"] = r["last"]
    for r in await conn.fetch("SELECT user_id FROM muse_user"):
        c = await canonical_user(conn, r["user_id"])
        people.setdefault(c, {"user_id": str(c), "ids": {str(c)}, "events_by_source": {}, "last_active": None})
    names = {r["user_id"]: r["display_name"] for r in await conn.fetch("SELECT user_id, display_name FROM muse_user")}
    out = []
    for c, p in people.items():
        out.append({
            "user_id": p["user_id"],
            "display_name": names.get(c),
            "linked_ids": sorted(p["ids"]),
            "events_by_source": p["events_by_source"],
            "last_active": p["last_active"].isoformat() if p["last_active"] else None,
        })
    return sorted(out, key=lambda p: p["last_active"] or "", reverse=True)


async def create_token(conn, user_id: uuid.UUID | None, label: str | None, can_write: bool) -> dict:
    token = "muse_" + secrets.token_urlsafe(32)
    if user_id is not None:
        user_id = await canonical_user(conn, user_id)
        await ensure_user(conn, user_id)
    row = await conn.fetchrow(
        "INSERT INTO api_token (token_hash, user_id, label, can_write) VALUES ($1, $2, $3, $4) RETURNING id, created_at",
        hash_secret(token), user_id, label, can_write,
    )
    return {
        "id": str(row["id"]),
        "token": token,
        "user_id": str(user_id) if user_id else None,
        "label": label,
        "can_write": can_write,
        "note": "Shown once. Use it as MUSE_TOKEN for the Muse MCP server, or as a Bearer token for the query API.",
    }


async def list_tokens(conn, user_id: uuid.UUID | None) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, user_id, label, can_write, created_at, last_used_at FROM api_token "
        "WHERE revoked_at IS NULL AND ($1::uuid IS NULL OR user_id = $1) ORDER BY created_at DESC",
        user_id,
    )
    return [
        {"id": str(r["id"]), "user_id": str(r["user_id"]) if r["user_id"] else None, "label": r["label"],
         "can_write": r["can_write"], "created_at": r["created_at"].isoformat(),
         "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None}
        for r in rows
    ]
