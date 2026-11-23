"""Cross-service tests: a real Postgres (migrations applied, TimescaleDB lines stripped when
the extension isn't installed), an in-memory Qdrant and a deterministic fake embedding model.

    MUSE_TEST_DATABASE_URL=postgresql://muse@127.0.0.1:55432/muse_test pytest tests

Skipped when no database URL is given. Services are loaded as separate packages (each one's
code lives in a package called `app`).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import math
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB_URL = os.environ.get("MUSE_TEST_DATABASE_URL")

# Must be set before the services read their environment at import time.
os.environ.setdefault("MUSE_DATABASE_URL", DB_URL or "postgresql://unused/none")
os.environ["MUSE_ADAPTER_KEYS"] = "mono:mono-key,jellyfin:jf-key"
os.environ["MUSE_ADMIN_TOKEN"] = "admin-secret"
os.environ["MUSE_QUERY_EMBEDDER"] = "none"
os.environ["MUSE_QUERY_EMBED_MOODS"] = "0"
os.environ["MUSE_OUTBOUND_ALLOW_PRIVATE"] = "example,cal.example"


# --- encryption ---------------------------------------------------------------------------
# Every test runs with encryption ON (a throwaway key), so the whole pipeline is exercised the
# way it runs in production.
sys.path.insert(0, str(ROOT / "shared"))
from muse_schema.crypto import generate_key_file, reset_vault_cache  # noqa: E402

os.environ.setdefault("MUSE_MASTER_KEY", generate_key_file())
os.environ.pop("MUSE_ENCRYPTION", None)
reset_vault_cache()
os.environ["MUSE_ALLOW_OPEN_READ"] = "true"  # route tests call the API without keys


from muse_testkit import FakeModel, load_module, load_package  # noqa: E402,F401


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def migrations_sql(has_timescale: bool) -> list[str]:
    out = []
    for f in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        sql = f.read_text()
        if not has_timescale:
            sql = "\n".join(l for l in sql.splitlines() if "timescaledb" not in l and "create_hypertable" not in l)
        out.append(sql)
    return out


@pytest.fixture
async def db():
    if not DB_URL:
        pytest.skip("MUSE_TEST_DATABASE_URL not set")
    import asyncpg

    conn = await asyncpg.connect(DB_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    has_ts = bool(await conn.fetchval("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'"))
    for sql in migrations_sql(has_ts):
        await conn.execute(sql)
    await conn.close()
    yield DB_URL
