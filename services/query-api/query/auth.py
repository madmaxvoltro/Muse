"""Who is calling, and for whom they may act.

* **admin**: `Authorization: Bearer <MUSE_ADMIN_TOKEN>`, or a household token (api_token
  without a user). Acts for anyone; says which person with `?user=<id>`.
* **person**: `Authorization: Bearer <token>` bound to one person (what an assistant's MCP
  connection uses). Acts for that person only, with all their linked ids.
* **adapter**: `X-Muse-Api-Key` (the service's ingestion key, e.g. Mono's) plus
  `X-Muse-User: <uuid>`: a source server acting for one of its own users, so it can show
  Muse's questions and hand out tokens without anyone seeing an admin key. Guard: the id must
  not carry events ingested from another source.

Tokens are stored as keyed digests (the vault's index key), never in the clear.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from muse_schema import IMPORT_SOURCES, identity
from muse_schema.config import key_list, secret
from muse_schema.crypto import P_TOKEN, get_vault

from .db import get_pool

TOKEN_PREFIX = "muse_"


def admin_token() -> str:
    return secret("MUSE_ADMIN_TOKEN", "") or ""


def adapter_keys() -> dict[str, str]:
    return key_list("MUSE_ADAPTER_KEYS")


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return get_vault().digest(token, P_TOKEN)


@dataclass
class Principal:
    kind: str  # admin | person | adapter
    user_id: uuid.UUID | None  # canonical id (person/adapter), None (admin)
    can_write: bool
    label: str
    source: str | None = None
    token_id: uuid.UUID | None = None

    @property
    def admin(self) -> bool:
        return self.kind == "admin"


async def _from_token(token: str) -> Principal:
    adm = admin_token()
    if adm and hmac.compare_digest(token.encode(), adm.encode()):
        return Principal("admin", None, True, "admin token")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE api_token SET last_used_at = now() WHERE token_hash = $1 AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now()) RETURNING id, user_id, can_write",
            token_digest(token),
        )
        if row is None:
            raise HTTPException(401, "invalid, expired or revoked token")
        if row["user_id"] is None:
            return Principal("admin", None, row["can_write"], "household token", token_id=row["id"])
        return Principal("person", await identity.canonical(conn, row["user_id"]), row["can_write"], "token", token_id=row["id"])


async def _from_adapter(key: str, user: str | None) -> Principal:
    source = None
    for k, src in adapter_keys().items():
        if hmac.compare_digest(k.encode(), key.strip().encode()):
            source = src
    if source is None:
        raise HTTPException(401, "invalid X-Muse-Api-Key")
    if source in ("import",):
        raise HTTPException(403, "the import key can't act for people")
    if not user:
        raise HTTPException(400, "adapter calls say which user with X-Muse-User")
    try:
        uid = uuid.UUID(user)
    except ValueError:
        raise HTTPException(400, "X-Muse-User must be a UUID") from None
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source != "muse":  # the muse key (Vesper, the MCP server) is trusted household-wide
            other = await conn.fetchval(
                "SELECT source FROM event WHERE user_id = $1 AND source <> ALL($2::text[]) LIMIT 1",
                uid, [source, "muse", *IMPORT_SOURCES],
            )
            if other:
                raise HTTPException(403, f"this user id belongs to {other}, not {source}")
        canonical = await identity.canonical(conn, uid)
    return Principal("adapter", canonical, True, f"{source} adapter", source=source)


async def current(
    request: Request,
    authorization: str | None = Header(default=None),
    x_muse_api_key: str | None = Header(default=None),
    x_muse_user: str | None = Header(default=None),
) -> Principal:
    if authorization and authorization.lower().startswith("bearer "):
        p = await _from_token(authorization[7:].strip())
    elif x_muse_api_key:
        p = await _from_adapter(x_muse_api_key, x_muse_user)
    else:
        raise HTTPException(401, "send Authorization: Bearer <token>, or X-Muse-Api-Key + X-Muse-User")
    request.state.principal = p
    return p


async def writer(p: Principal = Depends(current)) -> Principal:
    if not p.can_write:
        raise HTTPException(403, "this token is read-only")
    return p


async def admin_only(p: Principal = Depends(current)) -> Principal:
    if not p.admin:
        raise HTTPException(403, "admin only")
    return p


async def person(conn, p: Principal, user: str | None) -> uuid.UUID:
    """Canonical id of the person a request is about. `me` (or nothing) is the caller."""
    if user in (None, "", "me"):
        if p.user_id is None:
            raise HTTPException(400, "an admin token acts for the household: pass user=<id> (GET /v1/users)")
        return p.user_id
    try:
        uid = uuid.UUID(user)
    except ValueError:
        raise HTTPException(400, "user must be 'me' or a UUID") from None
    canonical = await identity.canonical(conn, uid)
    if not p.admin and canonical != p.user_id:
        raise HTTPException(403, "you can only act for yourself (and your linked ids)")
    return canonical
