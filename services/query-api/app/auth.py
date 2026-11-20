"""Who is calling, and for whom they may act.

Three kinds of caller:

* **admin** — `Authorization: Bearer $MUSE_ADMIN_TOKEN`, or an api_token row without a
  user (a household token). Acts for anyone; must say which user (`/v1/users` lists them).
* **user** — `Authorization: Bearer <api_token>` bound to one person. Acts for that person
  only (all their linked ids). This is what an assistant's MCP connection normally uses.
* **adapter** — `X-Muse-Api-Key` (an ingestion key from MUSE_ADAPTER_KEYS) plus
  `X-Muse-User: <uuid>`. A trusted source server (e.g. Mono) acting for one of *its own*
  users, so it can show Muse's questions in its UI, hand out a token or link accounts
  without the person ever seeing an admin key. Guard: the asserted id must not carry events
  from any other source, so an adapter can't act for another service's users.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from muse_schema import canonical_user

from .db import get_pool

ADMIN_TOKEN = os.environ.get("MUSE_ADMIN_TOKEN", "")
_ADAPTER_KEYS: dict[str, str] = {}
for pair in os.environ.get("MUSE_ADAPTER_KEYS", "").split(","):
    if ":" in pair:
        src, key = pair.split(":", 1)
        _ADAPTER_KEYS[key.strip()] = src.strip()


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class Principal:
    kind: str  # 'admin' | 'user' | 'adapter'
    user_id: uuid.UUID | None  # canonical id (user), asserted id (adapter), None (admin)
    can_write: bool
    label: str
    source: str | None = None  # adapter source name

    @property
    def admin(self) -> bool:
        return self.kind == "admin"


async def current_principal(
    authorization: str | None = Header(default=None),
    x_muse_api_key: str | None = Header(default=None),
    x_muse_user: str | None = Header(default=None),
) -> Principal:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if ADMIN_TOKEN and hmac.compare_digest(token, ADMIN_TOKEN):
            return Principal("admin", None, True, "admin token")
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE api_token SET last_used_at = now() WHERE token_hash = $1 AND revoked_at IS NULL "
                "RETURNING id, user_id, label, can_write",
                hash_secret(token),
            )
            if row is None:
                raise HTTPException(401, "invalid or revoked token")
            if row["user_id"] is None:
                return Principal("admin", None, row["can_write"], row["label"] or "household token")
            canonical = await canonical_user(conn, row["user_id"])
        return Principal("user", canonical, row["can_write"], row["label"] or "token")

    if x_muse_api_key:
        source = _ADAPTER_KEYS.get(x_muse_api_key.strip())
        if source is None:
            raise HTTPException(401, "invalid X-Muse-Api-Key")
        if not x_muse_user:
            raise HTTPException(400, "adapter calls must say which user with X-Muse-User")
        try:
            uid = uuid.UUID(x_muse_user)
        except ValueError:
            raise HTTPException(400, "X-Muse-User must be a UUID")
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Events Muse wrote itself (answers, ratings via the API) carry metadata.via and
            # may be about any source's items; only ingested events say whose id this is.
            other = await conn.fetchval(
                "SELECT source FROM event WHERE user_id = $1 AND source <> ALL($2::text[]) AND NOT metadata ? 'via' LIMIT 1",
                uid, [source, "muse"],
            )
        if other:
            raise HTTPException(403, f"user {uid} belongs to source '{other}', not '{source}'")
        return Principal("adapter", uid, True, f"{source} adapter", source=source)

    raise HTTPException(401, "send Authorization: Bearer <token>, or X-Muse-Api-Key + X-Muse-User")


async def writer(p: Principal = Depends(current_principal)) -> Principal:
    if not p.can_write:
        raise HTTPException(403, "this token is read-only")
    return p


async def admin_only(p: Principal = Depends(current_principal)) -> Principal:
    if not p.admin:
        raise HTTPException(403, "admin only")
    return p


async def resolve_user(conn, p: Principal, user: str | None) -> uuid.UUID:
    """Canonical id of the person a request is about. `me` (or nothing) = the caller."""
    if user in (None, "", "me"):
        if p.user_id is None:
            raise HTTPException(400, "an admin token acts for the whole household: pass user=<id> (see /v1/users)")
        return await canonical_user(conn, p.user_id)
    try:
        uid = uuid.UUID(user)
    except ValueError:
        raise HTTPException(400, "user must be 'me' or a UUID")
    canonical = await canonical_user(conn, uid)
    if p.admin:
        return canonical
    mine = await canonical_user(conn, p.user_id)
    if canonical != mine:
        raise HTTPException(403, "you can only act for yourself (and your linked identities)")
    return canonical
