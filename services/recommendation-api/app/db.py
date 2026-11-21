import json
import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def _codecs(conn: asyncpg.Connection) -> None:
    # jsonb in and out as Python objects: pass dicts, never json.dumps strings.
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=os.environ["MUSE_DATABASE_URL"], min_size=1, max_size=10, init=_codecs)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
