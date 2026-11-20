"""Applies db/migrations/*.sql on start-up.

Postgres only runs /docker-entrypoint-initdb.d on a brand-new volume, so every migration
added later used to need a manual psql step on existing installs. Each file is idempotent
(IF NOT EXISTS, ON CONFLICT), and applied files are recorded in schema_migration, so this is
safe to run on every start and on databases initialised by the entrypoint.

Set MUSE_AUTO_MIGRATE=false to manage the schema yourself.
"""

import logging
import os
from pathlib import Path

import asyncpg

logger = logging.getLogger("muse.migrate")

MIGRATIONS_DIR = Path(os.environ.get("MUSE_MIGRATIONS_DIR", "/migrations"))
# Two services may start at once; this key serialises them.
_LOCK_KEY = 0x6D757365  # "muse"


def pending(files: list[str], applied: set[str]) -> list[str]:
    """Migration files not applied yet, in filename order (001_, 002_, ...)."""
    return sorted(f for f in files if f.endswith(".sql") and f not in applied)


async def migrate(pool: asyncpg.Pool, directory: Path = MIGRATIONS_DIR) -> list[str]:
    if os.environ.get("MUSE_AUTO_MIGRATE", "true").lower() in ("0", "false", "no"):
        return []
    if not directory.is_dir():
        logger.warning("no migrations directory at %s, skipping", directory)
        return []
    done: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            applied = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migration")}
            for name in pending([p.name for p in directory.iterdir()], applied):
                sql = (directory / name).read_text()
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migration (name) VALUES ($1) ON CONFLICT DO NOTHING", name)
                logger.info("applied migration %s", name)
                done.append(name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    return done
