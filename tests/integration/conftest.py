"""Integration fixtures: a throwaway Postgres database with all migrations applied, an
in-memory Qdrant, and a deterministic bag-of-words embedder (no model download).

Set MUSE_TEST_PG (default: postgresql://postgres@localhost:5433/postgres via /tmp socket) to a
server where the test may create databases. Tests are skipped when none is reachable.
TimescaleDB isn't required: the hypertable lines are stripped for the test database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
ADMIN_DSN = os.environ.get("MUSE_TEST_PG", "postgresql://postgres@/postgres?host=/tmp&port=5433")


class FakeEmbedder:
    name = "fake-bow-128"
    dim = 128

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            if len(tok) < 3:
                continue
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed(self, texts):
        return np.stack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text):
        return self._one(text)


async def _json_codecs(conn):
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@pytest.fixture
def db_dsn():
    asyncpg = pytest.importorskip("asyncpg")
    name = f"muse_test_{uuid.uuid4().hex[:8]}"

    async def setup():
        admin = await asyncpg.connect(ADMIN_DSN)
        await admin.execute(f"CREATE DATABASE {name}")
        await admin.close()
        dsn = re.sub(r"/postgres(\?|$)", f"/{name}\\1", ADMIN_DSN)
        conn = await asyncpg.connect(dsn)
        for f in sorted((ROOT / "db" / "migrations").glob("*.sql")):
            sql = "\n".join(l for l in f.read_text().splitlines() if "timescaledb" not in l and "create_hypertable" not in l)
            await conn.execute(sql)
        await conn.close()
        return dsn

    try:
        dsn = asyncio.run(setup())
    except (OSError, Exception) as exc:  # no server
        pytest.skip(f"no test Postgres reachable: {exc}")
    yield dsn

    async def teardown():
        admin = await asyncpg.connect(ADMIN_DSN)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()

    asyncio.run(teardown())
