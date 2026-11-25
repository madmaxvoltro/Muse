"""Least privilege, migrations, forget/export, backups and key rotation."""

import asyncio
import io
import json
import re
import sys
import uuid

import asyncpg
import pytest

from conftest import ROOT, FakeEmbedder, _json_codecs
from fixtures import USER_A, FakeTMDB, seed

sys.path.insert(0, str(ROOT / "services" / "admin"))
sys.path.insert(0, str(ROOT / "services" / "embedding-worker"))

PASSWORDS = {r: f"pw-{r}-{uuid.uuid4().hex[:8]}" for r in ("muse_ingest", "muse_reco", "muse_worker", "muse_curator")}


def _role_dsn(dsn: str, role: str) -> str:
    return re.sub(r"postgresql://[^@]*@", f"postgresql://{role}:{PASSWORDS[role]}@", dsn) if "@" in dsn else dsn


@pytest.fixture
def admin_env(db_dsn, monkeypatch):
    monkeypatch.setenv("MUSE_DATABASE_URL", db_dsn)
    monkeypatch.setenv("MUSE_MIGRATIONS_DIR", str(ROOT / "db" / "migrations"))
    monkeypatch.setenv("MUSE_NO_TIMESCALE", "1")
    for role, pw in PASSWORDS.items():
        monkeypatch.setenv(f"MUSE_DB_PASSWORD_{role.upper()}", pw)
    import admin

    return admin, db_dsn


def test_migrate_roles_and_least_privilege(admin_env):
    admin, dsn = admin_env
    host_dsn = dsn.replace("postgresql://postgres@/", "postgresql://postgres@127.0.0.1:5433/").split("?")[0]

    async def go():
        conn = await admin.connect()
        res = await admin.migrate(conn)
        again = await admin.migrate(conn)  # idempotent: nothing pending the second time
        async with conn.transaction():
            await seed(conn)
        await conn.close()

        out = {"res": res, "again": again}
        # worker as muse_worker
        import worker as worker_mod
        from qdrant_client import QdrantClient

        def role_dsn(role):
            return f"postgresql://{role}:{PASSWORDS[role]}@127.0.0.1:5433/{host_dsn.rsplit('/', 1)[1]}"

        wpool = await asyncpg.create_pool(role_dsn("muse_worker"), min_size=1, max_size=3, init=_json_codecs)
        w = worker_mod.Worker(wpool, QdrantClient(":memory:"), FakeEmbedder(), tmdb=FakeTMDB())
        await w.prepare()
        out["report"] = await w.run_once()
        await wpool.close()

        denied = {}
        for role, sql in (("muse_ingest", "SELECT * FROM taste_profile"), ("muse_ingest", "SELECT * FROM item"),
                          ("muse_reco", "DELETE FROM event"), ("muse_reco", "UPDATE taste_profile SET profile = '{}'"),
                          ("muse_curator", "INSERT INTO event (user_id, source, item_type, action) VALUES (gen_random_uuid(), 'x', 'movie', 'played')"),
                          ("muse_worker", "DROP TABLE event"), ("muse_worker", "CREATE TABLE x (a int)")):
            c = await asyncpg.connect(role_dsn(role))
            try:
                await c.execute(sql)
                denied[(role, sql)] = False
            except (asyncpg.InsufficientPrivilegeError, asyncpg.exceptions.InsufficientPrivilegeError):
                denied[(role, sql)] = True
            finally:
                await c.close()
        out["denied"] = denied
        return out

    out = asyncio.run(go())
    assert "010_operations.sql" in out["res"]["applied"]
    assert set(out["res"]["roles"]) == set(PASSWORDS)
    assert out["again"]["applied"] == []
    assert out["report"]["profiles"] >= 1  # the whole worker cycle ran with least privilege
    assert all(out["denied"].values()), [k for k, v in out["denied"].items() if not v]


def test_export_forget_backup_and_rotation(admin_env, monkeypatch):
    admin, dsn = admin_env
    from muse_schema.crypto import Vault, add_key, generate_key_file, get_vault, reset_vault_cache

    async def go():
        conn = await admin.connect()
        await admin.migrate(conn)
        async with conn.transaction():
            await seed(conn)
        exported = await admin.export_user(conn, str(USER_A))

        # rotate: add a key, reseal, every token now uses the new key and still opens
        import os

        text = os.environ["MUSE_MASTER_KEY"]
        new_text, kid = add_key(text)
        monkeypatch.setenv("MUSE_MASTER_KEY", new_text)
        reset_vault_cache()
        resealed = await admin.reseal(conn)
        stale = await conn.fetchval("SELECT count(*) FROM event WHERE metadata ? '_enc' AND metadata->>'_enc' NOT LIKE $1", f"m1.{kid}.%")
        exported_after = await admin.export_user(conn, str(USER_A))

        forgot = await admin.forget_user(conn, str(USER_A), )
        left = await conn.fetchval("SELECT count(*) FROM event WHERE user_id = $1", USER_A)
        await conn.close()
        return exported, resealed, stale, exported_after, forgot, left, kid

    exported, resealed, stale, exported_after, forgot, left, kid = asyncio.run(go())
    titles = {e["metadata"].get("title") for e in exported["events"] if e["metadata"]}
    assert "Crime Film 0" in titles and any(e["item_id"] == "jf100" for e in exported["events"])
    assert resealed["event.metadata/device_context"] > 0 and stale == 0
    assert len(exported_after["events"]) == len(exported["events"])
    assert forgot["event"] > 0 and left == 0

    # encrypted backup stream: roundtrip, and tampering is detected
    v = get_vault()
    src = b"pg_dump bytes " * 200_000
    enc, dec = io.BytesIO(), io.BytesIO()
    v.encrypt_stream(io.BytesIO(src), enc)
    v.decrypt_stream(io.BytesIO(enc.getvalue()), dec)
    assert dec.getvalue() == src and b"pg_dump" not in enc.getvalue()
    bad = bytearray(enc.getvalue())
    bad[100] ^= 1
    from muse_schema.crypto import DecryptionError

    with pytest.raises(DecryptionError):
        v.decrypt_stream(io.BytesIO(bytes(bad)), io.BytesIO())
    with pytest.raises(DecryptionError):  # truncated
        v.decrypt_stream(io.BytesIO(enc.getvalue()[:-200]), io.BytesIO())


def test_legacy_plaintext_rows_get_encrypted(admin_env):
    admin, dsn = admin_env

    async def go():
        conn = await admin.connect()
        await admin.migrate(conn)
        await conn.execute(
            """INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, metadata)
               VALUES ($1, 'jellyfin', 'plain-id-1', 'movie', 'completed', 1.0, $2::jsonb)""",
            USER_A, {"title": "Legacy Title", "dedupe_key": "k1"},
        )
        n = await admin.encrypt_legacy(conn)
        row = await conn.fetchrow("SELECT source_item_id, metadata FROM event")
        await conn.close()
        return n, row

    n, row = asyncio.run(go())
    assert n == 1 and row["source_item_id"].startswith("b1")
    assert "Legacy Title" not in json.dumps(row["metadata"]) and row["metadata"]["dedupe_key"].startswith("b1")
