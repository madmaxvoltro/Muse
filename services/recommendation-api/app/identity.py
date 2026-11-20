"""Linked identities: one person across Vesper, Mono and other Muse sources.

Each app keeps using its own Muse user_id. Linking pairs two of them through a short code:
the app that *shows* the code owns the canonical identity, the app where it is *entered*
becomes an alias. From then on:

  - the ingestion API writes the alias' new events onto the canonical user;
  - linking moves the alias' existing events over (metadata.original_user_id remembers
    where they came from, and ingested_at is bumped so the embedding worker rebuilds the
    canonical taste vectors);
  - every read endpoint resolves an alias to its canonical user first, so either app can
    keep asking with its own id;
  - unlinking moves the alias' events back.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from qdrant_client import QdrantClient
from qdrant_client.http.models import FieldCondition, Filter, FilterSelector, MatchValue

CODE_TTL_MINUTES = 15
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I: read off a TV across the room


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    return f"{raw[:3]}-{raw[3:]}"


def normalise_code(code: str) -> str:
    c = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{c[:3]}-{c[3:]}" if len(c) == 6 else c


async def canonical(conn_or_pool, user_id: str) -> str:
    row = await conn_or_pool.fetchval("SELECT user_id FROM identity_link WHERE alias_user_id = $1", uuid.UUID(user_id))
    return str(row) if row else str(uuid.UUID(user_id))


async def canonical_many(pool: asyncpg.Pool, user_ids: list[str]) -> list[str]:
    out: list[str] = []
    for u in user_ids:
        c = await canonical(pool, u)
        if c not in out:
            out.append(c)
    return out


async def create_code(pool: asyncpg.Pool, user_id: str, source: str | None, label: str | None) -> dict:
    owner = await canonical(pool, user_id)
    code = new_code()
    expires = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM identity_link_code WHERE expires_at < now() OR user_id = $1", uuid.UUID(owner))
        await conn.execute(
            "INSERT INTO identity_link_code (code, user_id, source, label, expires_at) VALUES ($1, $2, $3, $4, $5)",
            code, uuid.UUID(owner), source, label, expires,
        )
    return {"code": code, "user_id": owner, "expires_at": expires.isoformat()}


async def redeem(pool: asyncpg.Pool, qdrant: QdrantClient, code: str, alias_user_id: str, source: str | None, label: str | None) -> dict:
    alias = str(uuid.UUID(alias_user_id))
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM identity_link_code WHERE code = $1 AND expires_at > now() RETURNING user_id, source, label",
                normalise_code(code),
            )
            if row is None:
                raise LookupError("code unknown or expired")
            owner = str(row["user_id"])
            alias = await canonical(conn, alias)  # an alias of an alias: link its canonical
            if alias == owner:
                return {"user_id": owner, "linked": [], "note": "already the same identity"}
            # Everything the alias had (and its own aliases) moves to the owner.
            moved_aliases = [str(r["alias_user_id"]) for r in await conn.fetch("SELECT alias_user_id FROM identity_link WHERE user_id = $1", uuid.UUID(alias))]
            await conn.execute("UPDATE identity_link SET user_id = $1 WHERE user_id = $2", uuid.UUID(owner), uuid.UUID(alias))
            await conn.execute(
                "INSERT INTO identity_link (alias_user_id, user_id, source, label) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (alias_user_id) DO UPDATE SET user_id = EXCLUDED.user_id, source = EXCLUDED.source, label = EXCLUDED.label, linked_at = now()",
                uuid.UUID(alias), uuid.UUID(owner), source, label,
            )
            moved = await conn.execute(
                """
                UPDATE event SET user_id = $1, ingested_at = now(),
                  metadata = CASE WHEN metadata ? 'original_user_id' THEN metadata
                                  ELSE metadata || jsonb_build_object('original_user_id', $2::text) END
                WHERE user_id = $3
                """,
                uuid.UUID(owner), alias, uuid.UUID(alias),
            )
            await conn.execute(
                "INSERT INTO user_overlay (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uuid.UUID(owner)
            )
    _drop_taste(qdrant, alias)
    return {"user_id": owner, "linked": [alias, *moved_aliases], "events_moved": int(moved.split()[-1]), "owner_source": row["source"], "owner_label": row["label"]}


async def links(pool: asyncpg.Pool, user_id: str) -> dict:
    owner = await canonical(pool, user_id)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT alias_user_id, source, label, linked_at FROM identity_link WHERE user_id = $1 ORDER BY linked_at", uuid.UUID(owner)
        )
    return {
        "user_id": owner,
        "is_alias": owner != str(uuid.UUID(user_id)),
        "links": [{"user_id": str(r["alias_user_id"]), "source": r["source"], "label": r["label"], "linked_at": r["linked_at"].isoformat()} for r in rows],
    }


async def unlink(pool: asyncpg.Pool, qdrant: QdrantClient, user_id: str, alias_user_id: str) -> dict:
    owner = await canonical(pool, user_id)
    alias = str(uuid.UUID(alias_user_id))
    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted = await conn.fetchval(
                "DELETE FROM identity_link WHERE alias_user_id = $1 AND user_id = $2 RETURNING alias_user_id", uuid.UUID(alias), uuid.UUID(owner)
            )
            if deleted is None:
                raise LookupError("not linked")
            moved = await conn.execute(
                "UPDATE event SET user_id = $1, ingested_at = now(), metadata = metadata - 'original_user_id' "
                "WHERE user_id = $2 AND metadata->>'original_user_id' = $3",
                uuid.UUID(alias), uuid.UUID(owner), alias,
            )
            # The owner's vectors are rebuilt from what's left on the next worker cycle.
            await conn.execute("UPDATE event SET ingested_at = now() WHERE user_id = $1 AND \"timestamp\" > now() - interval '1 day'", uuid.UUID(owner))
    return {"user_id": owner, "unlinked": alias, "events_moved": int(moved.split()[-1])}


def _drop_taste(qdrant: QdrantClient, user_id: str) -> None:
    try:
        qdrant.delete(
            collection_name="taste_vectors",
            points_selector=FilterSelector(filter=Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])),
        )
    except Exception:  # noqa: BLE001 - best effort; stale alias vectors are never read again
        pass


async def delete_user(pool: asyncpg.Pool, qdrant: QdrantClient, user_id: str) -> dict:
    """Per-user wipe (a Vesper profile was deleted): events, overlay, links, curator log and
    taste vectors. Linked aliases are unlinked, not wiped: they belong to other apps."""
    uid = uuid.UUID(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            aliases = [str(r["alias_user_id"]) for r in await conn.fetch("DELETE FROM identity_link WHERE user_id = $1 RETURNING alias_user_id", uid)]
            for a in aliases:  # give linked apps their own events back before the wipe
                await conn.execute(
                    "UPDATE event SET user_id = $1, ingested_at = now(), metadata = metadata - 'original_user_id' WHERE user_id = $2 AND metadata->>'original_user_id' = $3",
                    uuid.UUID(a), uid, a,
                )
            events = await conn.execute("DELETE FROM event WHERE user_id = $1", uid)
            await conn.execute("DELETE FROM identity_link WHERE alias_user_id = $1", uid)
            await conn.execute("DELETE FROM user_overlay WHERE user_id = $1", uid)
            await conn.execute("DELETE FROM identity_mapping WHERE user_id = $1", uid)
            await conn.execute("DELETE FROM curator_download_log WHERE user_id = $1", uid)
            await conn.execute("DELETE FROM identity_link_code WHERE user_id = $1", uid)
            await conn.execute("DELETE FROM watch_group WHERE $1 = ANY(members)", uid)
    _drop_taste(qdrant, str(uid))
    return {"user_id": str(uid), "events_deleted": int(events.split()[-1]), "aliases_released": aliases}


async def note_group(pool: asyncpg.Pool, user_ids: list[str]) -> list[str]:
    members = sorted(set(await canonical_many(pool, user_ids)))
    if len(members) < 2:
        return members
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO watch_group (members, sessions, last_seen_at) VALUES ($1::uuid[], 1, now()) "
            "ON CONFLICT (members) DO UPDATE SET sessions = watch_group.sessions + 1, last_seen_at = now()",
            [uuid.UUID(m) for m in members],
        )
    return members


