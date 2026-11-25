"""Linked identities: one person, several ids (Jellyfin GUID, Mono account, ...).

Events keep the id they were written with. Readers resolve an id to its canonical id and read
the events of every id that belongs to it. Linking or unlinking therefore never rewrites
history and is undone by deleting one user_alias row. Databases without migration 011 behave
as if nobody is linked.
"""

from __future__ import annotations

import uuid

import asyncpg


def _uuid(v) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


async def canonical(conn: asyncpg.Connection, user_id) -> uuid.UUID:
    uid = _uuid(user_id)
    try:
        row = await conn.fetchval("SELECT user_id FROM user_alias WHERE alias_id = $1", uid)
    except asyncpg.UndefinedTableError:
        return uid
    return row or uid


async def members(conn: asyncpg.Connection, user_id) -> list[uuid.UUID]:
    """Every id of the person `user_id` belongs to, canonical first."""
    root = await canonical(conn, user_id)
    try:
        rows = await conn.fetch("SELECT alias_id FROM user_alias WHERE user_id = $1 ORDER BY created_at", root)
    except asyncpg.UndefinedTableError:
        return [root]
    return [root, *(r["alias_id"] for r in rows)]


async def alias_map(conn: asyncpg.Connection) -> dict[str, str]:
    """alias id -> canonical id, as strings (for batch work in the worker)."""
    try:
        rows = await conn.fetch("SELECT alias_id, user_id FROM user_alias")
    except asyncpg.UndefinedTableError:
        return {}
    return {str(r["alias_id"]): str(r["user_id"]) for r in rows}
