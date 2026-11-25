import json

import asyncpg
from muse_schema.config import database_url

_pool: asyncpg.Pool | None = None


async def _codecs(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=database_url(), min_size=1, max_size=8, init=_codecs)
    return _pool


def set_pool(pool: asyncpg.Pool | None) -> None:
    """Tests (and embedding the API elsewhere) hand in their own pool."""
    global _pool
    _pool = pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
