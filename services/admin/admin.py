"""Muse admin tool. Runs inside the `admin` image (the CLI in ./muse calls it through
`docker compose run`), with the database superuser and the master key.

  migrate           apply pending migrations, (re)create least-privilege roles, encrypt any
                    rows written before encryption existed. Runs automatically on every start.
  status [--json]   worker heartbeat, catalog/embedding progress, queue (counts only)
  users             user ids with event counts and last activity (no content)
  encrypt-stream    stdin -> stdout, AES-256-GCM (backups)
  decrypt-stream    stdin -> stdout (restore)
  forget-user ID    delete everything about one person (events, profiles, answers, impressions...)
  export-user ID    everything about one person, decrypted, as JSON on stdout
  reseal            re-encrypt every sealed value with the newest key (after `muse rotate-keys`)
  rebuild           drop derived state (catalog, profiles, vectors); the worker rebuilds it from events
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import asyncpg
from muse_schema.config import database_url, secret
from muse_schema.crypto import (
    P_ADMIN_NOTE,
    P_ANSWER,
    P_CANON_DATA,
    P_CURATOR,
    P_DEDUPE,
    P_EVENT_METADATA,
    P_ITEM_DATA,
    P_OVERLAY,
    P_PAIRING,
    P_PAYLOAD,
    P_PROFILE,
    P_QUESTION,
    event_item_bid,
    get_vault,
    is_blind,
    is_token,
)
from muse_schema.status import collect

MIGRATIONS = Path(os.environ.get("MUSE_MIGRATIONS_DIR", "/app/migrations"))

# role -> grants. Each service connects as its own role with only what it needs, so a
# compromised service can't read or change more than its job requires.
ROLES: dict[str, list[str]] = {
    "muse_ingest": [
        "GRANT INSERT ON event TO {r}",
        "GRANT SELECT (user_id, source, metadata) ON event TO {r}",  # idempotent inserts
        "GRANT SELECT, UPDATE ON question_log TO {r}",
        "GRANT SELECT ON user_overlay, user_alias TO {r}",  # incognito mode, linked identities
    ],
    "muse_reco": [
        "GRANT SELECT ON event, item, canon_item, taste_profile, user_overlay, ranker_model, question_log, soon_gone, embedding_worker_state, schema_migrations TO {r}",
        "GRANT INSERT ON impression TO {r}",
        "GRANT SELECT ON impression TO {r}",
        "GRANT USAGE ON SEQUENCE impression_id_seq TO {r}",
        "GRANT INSERT, UPDATE ON question_log TO {r}",
        "GRANT UPDATE ON soon_gone TO {r}",
        "GRANT INSERT ON event TO {r}",  # Soon Gone whitelist writes a favourite
        # multi-space engine (routes_multispace.py): people, facts, context, groups, sound
        "GRANT SELECT ON user_alias, muse_user, profile_fact, context_signal, room_presence, person_ref, goal, group_session, group_pick, "
        "item_audio, item_transition, taste_model, taste_facet, engine_ranker_model TO {r}",
        "GRANT SELECT, INSERT ON engine_impression TO {r}",
    ],
    "muse_worker": [
        "GRANT SELECT ON event, impression, user_overlay TO {r}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON item, canon_item, taste_profile, question_log, ranker_model, embedding_worker_state TO {r}",
        "GRANT USAGE ON SEQUENCE ranker_model_id_seq TO {r}",
    ],
    "muse_curator": [
        "GRANT SELECT ON event, item, taste_profile, user_overlay, user_alias TO {r}",
        "GRANT SELECT, INSERT, UPDATE ON curator_managed_item, curator_download_log, soon_gone, curator_setting, curator_activity, "
        "admin_notification, notification_log, webhook_delivery TO {r}",
        "GRANT SELECT ON webhook TO {r}",
    ],
    # The multi-space engine's own services.
    "muse_vector": [  # services/embedding-worker/vector_worker.py: content/taste vectors, deep taste, learned ranker
        "GRANT SELECT ON event, profile_fact, user_alias, item_audio, user_overlay TO {r}",
        "GRANT SELECT, UPDATE ON vector_worker_state TO {r}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON taste_dirty, taste_facet, taste_model, engine_ranker_model TO {r}",
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON item_transition TO {r}",
        "GRANT SELECT, UPDATE ON engine_impression TO {r}",
        "GRANT USAGE ON SEQUENCE engine_ranker_model_id_seq TO {r}",
    ],
    "muse_query": [  # services/query-api: people, facts, questions, tokens, imports, privacy tools
        "GRANT SELECT, INSERT, DELETE ON event TO {r}",  # ratings/answers/imports; forget-history
        "GRANT SELECT, INSERT, UPDATE, DELETE ON access_log, api_token, calendar_feed, context_signal, feature_suggestion, goal, group_pick, "
        "group_session, import_job, link_code, muse_user, notification_log, person_ref, profile_fact, question, retention_policy, room_presence, "
        "taste_dirty, user_alias, user_overlay, wanted, webhook, webhook_delivery, admin_notification, pairing_code TO {r}",
        # admin curator controls from Vesper/Mono: keep, remove now, pause, budget
        "GRANT SELECT, UPDATE ON curator_managed_item, curator_setting, soon_gone TO {r}",
        "GRANT SELECT, INSERT ON curator_activity TO {r}",
        "GRANT SELECT, DELETE ON engine_impression TO {r}",
        "GRANT SELECT ON taste_facet, taste_model, catalog_artist, catalog_release, item_audio TO {r}",
        "GRANT USAGE ON SEQUENCE access_log_id_seq TO {r}",
    ],
    "muse_catalog": [  # services/catalog: music beyond the library, new releases, the wanted list
        "GRANT SELECT, INSERT ON event TO {r}",
        "GRANT SELECT ON user_alias, webhook TO {r}",
        "GRANT SELECT, INSERT, UPDATE ON catalog_artist, catalog_name, catalog_release, wanted, item_audio, notification_log, webhook_delivery, admin_notification TO {r}",
    ],
    "muse_audio": [  # services/audio-analyzer
        "GRANT SELECT ON event TO {r}",
        "GRANT SELECT, INSERT, UPDATE ON item_audio TO {r}",
    ],
    "muse_scheduler": [  # services/scheduler: weather, calendars, webhooks, digest, goals, retention
        "GRANT SELECT, DELETE ON event, engine_impression, access_log TO {r}",
        "GRANT SELECT ON question, retention_policy, user_alias, wanted TO {r}",
        "GRANT SELECT, UPDATE ON calendar_feed, goal, webhook TO {r}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON context_signal, webhook_delivery, notification_log TO {r}",
    ],
}


async def connect() -> asyncpg.Connection:
    conn = await asyncpg.connect(database_url())
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    return conn


# ---- migrate ---------------------------------------------------------------------------------

async def migrate(conn: asyncpg.Connection) -> dict:
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name in done:
            continue
        sql = f.read_text()
        if os.environ.get("MUSE_NO_TIMESCALE") == "1":  # tests without the extension
            sql = "\n".join(l for l in sql.splitlines() if "timescaledb" not in l and "create_hypertable" not in l)
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING", f.name)
        applied.append(f.name)
    roles = await ensure_roles(conn)
    legacy = await encrypt_legacy(conn)
    return {"applied": applied, "roles": roles, "encrypted_legacy_rows": legacy}


async def ensure_roles(conn: asyncpg.Connection) -> list[str]:
    made = []
    await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    for role, grants in ROLES.items():
        password = secret(f"MUSE_DB_PASSWORD_{role.upper()}")
        if not password:
            continue  # role not configured (development)
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        # passwords can't be bound parameters in DDL; quote_literal keeps it safe
        pw = await conn.fetchval("SELECT quote_literal($1)", password)
        if exists:
            await conn.execute(f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {pw}")
        else:
            await conn.execute(f"CREATE ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {pw}")
        db = await conn.fetchval("SELECT current_database()")
        await conn.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {role}')
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        for g in grants:
            await conn.execute(g.format(r=role))
        made.append(role)
    return made


async def encrypt_legacy(conn: asyncpg.Connection, batch: int = 2000) -> int:
    """Rows written before encryption: seal metadata, blind the item id and dedupe key. Derived
    state is then rebuilt from the (now encrypted) events."""
    vault = get_vault()
    if not vault.enabled:
        return 0
    total = 0
    while True:
        rows = await conn.fetch(
            """
            SELECT id, "timestamp", source, source_item_id, metadata, device_context FROM event
            WHERE (source_item_id IS NOT NULL AND source_item_id NOT LIKE 'b1%')
               OR (metadata <> '{}'::jsonb AND NOT (metadata ? '_enc'))
            LIMIT $1
            """,
            batch,
        )
        if not rows:
            break
        for r in rows:
            md = dict(r["metadata"] or {})
            dedupe = md.pop("dedupe_key", None)
            sid = r["source_item_id"]
            if sid is not None and not is_blind(sid):
                md["_sid"] = sid
                sid = event_item_bid(vault, r["source"], sid)
            stored = vault.seal_json(md, P_EVENT_METADATA) if md else {}
            if dedupe is not None:
                stored["dedupe_key"] = dedupe if is_blind(dedupe) else vault.bid(f"{r['source']}:{dedupe}", P_DEDUPE)
            dc = r["device_context"]
            if dc is not None and not (isinstance(dc, dict) and "_enc" in dc):
                dc = vault.seal_json(dc, P_EVENT_METADATA)
            await conn.execute(
                'UPDATE event SET source_item_id = $3, metadata = $4::jsonb, device_context = $5::jsonb WHERE id = $1 AND "timestamp" = $2',
                r["id"], r["timestamp"], sid, stored, dc,
            )
        total += len(rows)
    if total:
        await rebuild(conn, qdrant_reset=True)
    return total


# ---- derived state --------------------------------------------------------------------------

async def rebuild(conn: asyncpg.Connection, qdrant_reset: bool = True) -> None:
    await conn.execute("TRUNCATE item, canon_item, taste_profile")
    await conn.execute("UPDATE embedding_worker_state SET last_processed_at = '1970-01-01T00:00:00Z', last_full_profile_at = NULL")
    if qdrant_reset:
        q = _qdrant()
        if q is not None:
            from muse_taste.store import ITEMS_COLLECTION

            if q.collection_exists(ITEMS_COLLECTION):
                q.delete_collection(ITEMS_COLLECTION)


def _qdrant():
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        return None
    url = os.environ.get("MUSE_QDRANT_URL")
    return QdrantClient(url=url, api_key=secret("MUSE_QDRANT_API_KEY") or None) if url else None


# ---- people ---------------------------------------------------------------------------------

PERSONAL_TABLES = ["event", "taste_profile", "impression", "question_log", "user_overlay", "curator_download_log"]


async def forget_user(conn: asyncpg.Connection, user_id: str) -> dict:
    uid = uuid.UUID(user_id)
    counts = {}
    async with conn.transaction():
        for t in PERSONAL_TABLES:
            res = await conn.execute(f"DELETE FROM {t} WHERE user_id = $1", uid)
            counts[t] = int(res.split()[-1])
        res = await conn.execute("UPDATE soon_gone SET whitelisted_by = NULL WHERE whitelisted_by = $1", uid)
        # Others' profiles used this person for household recommendations: refresh everyone.
        await conn.execute("UPDATE embedding_worker_state SET last_full_profile_at = NULL")
    return counts


async def export_user(conn: asyncpg.Connection, user_id: str) -> dict:
    """GDPR-style export: everything Muse holds about one person, decrypted."""
    vault = get_vault()
    uid = uuid.UUID(user_id)
    events = []
    for r in await conn.fetch('SELECT * FROM event WHERE user_id = $1 ORDER BY "timestamp"', uid):
        md = vault.open_json(r["metadata"], P_EVENT_METADATA) or {}
        if isinstance(md, dict):
            md.pop("dedupe_key", None)
        events.append({
            "timestamp": r["timestamp"].isoformat(), "source": r["source"], "item_type": r["item_type"], "action": r["action"],
            "item_id": (md or {}).pop("_sid", None) if isinstance(md, dict) else None, "progress_pct": r["progress_pct"], "metadata": md,
        })
    profiles = {r["domain"]: vault.open_json(r["profile"], P_PROFILE) for r in await conn.fetch("SELECT domain, profile FROM taste_profile WHERE user_id = $1", uid)}
    questions = [
        {"kind": r["kind"], "asked_at": r["asked_at"].isoformat(), "status": r.get("status"),
         "question": vault.open_json(r["payload"], P_QUESTION), "answer": vault.open_json(r.get("answer"), P_ANSWER)}
        for r in [dict(x) for x in await conn.fetch("SELECT * FROM question_log WHERE user_id = $1", uid)]
    ]
    overlay = await conn.fetchrow("SELECT * FROM user_overlay WHERE user_id = $1", uid)
    overlay = dict(overlay) if overlay else None
    if overlay:
        overlay["pinned_topics"] = vault.open_json(overlay.get("pinned_topics"), P_OVERLAY)
        overlay = {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, uuid.UUID) else v) for k, v in overlay.items()}
    impressions = await conn.fetchval("SELECT count(*) FROM impression WHERE user_id = $1", uid)
    return {"user_id": user_id, "events": events, "taste_profiles": profiles, "questions": questions, "overlay": overlay,
            "impressions_logged": impressions}


# ---- key rotation ---------------------------------------------------------------------------

SEALED_COLUMNS = [
    ("event", ["metadata", "device_context"], P_EVENT_METADATA, ("id", "timestamp")),
    ("item", ["metadata", "enrichment", "llm_profile"], P_ITEM_DATA, ("item_key",)),
    ("canon_item", ["data"], P_CANON_DATA, ("canon_key",)),
    ("taste_profile", ["profile"], P_PROFILE, ("user_id", "domain")),
    ("question_log", ["payload"], P_QUESTION, ("id",)),
    ("question_log", ["answer"], P_ANSWER, ("id",)),
    ("user_overlay", ["pinned_topics"], P_OVERLAY, ("user_id",)),
    ("curator_managed_item", ["data"], P_CURATOR, ("id",)),
    ("curator_activity", ["data"], P_CURATOR, ("id",)),
    ("admin_notification", ["data"], P_ADMIN_NOTE, ("id",)),
    ("pairing_code", ["payload"], P_PAIRING, ("code_hash",)),
]


async def reseal(conn: asyncpg.Connection) -> dict:
    vault = get_vault()
    out: dict[str, int] = {}
    for table, cols, purpose, pk in SEALED_COLUMNS:
        n = 0
        for col in cols:
            where = " AND ".join(f'"{c}" = ${i + 2}' for i, c in enumerate(pk))
            for r in await conn.fetch(f'SELECT {", ".join(chr(34) + c + chr(34) for c in pk)}, "{col}" AS v FROM {table} WHERE "{col}" ? \'_enc\''):
                v = r["v"]
                if not (isinstance(v, dict) and vault.needs_reseal(v.get("_enc"))):
                    continue
                new = {**v, "_enc": vault.seal(vault.open(v["_enc"], purpose), purpose)}
                await conn.execute(f'UPDATE {table} SET "{col}" = $1::jsonb WHERE {where}', new, *[r[c] for c in pk])
                n += 1
        out[f"{table}.{'/'.join(cols)}"] = n
    q = _qdrant()
    if q is not None:
        from muse_taste.store import ITEMS_COLLECTION

        n, offset = 0, None
        if q.collection_exists(ITEMS_COLLECTION):
            while True:
                pts, offset = q.scroll(ITEMS_COLLECTION, limit=500, offset=offset, with_payload=True)
                for p in pts:
                    enc = (p.payload or {}).get("enc")
                    if is_token(enc) and vault.needs_reseal(enc):
                        q.set_payload(ITEMS_COLLECTION, payload={"enc": vault.seal(vault.open(enc, P_PAYLOAD), P_PAYLOAD)}, points=[p.id])
                        n += 1
                if offset is None:
                    break
        out["qdrant.payload"] = n
    return out


# ---- users / status -------------------------------------------------------------------------

async def users(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch(
        "SELECT user_id, count(*) AS events, max(\"timestamp\") AS last_activity FROM event WHERE action <> 'cataloged' GROUP BY user_id ORDER BY last_activity DESC"
    )
    return [{"user_id": str(r["user_id"]), "events": r["events"], "last_activity": r["last_activity"].isoformat()} for r in rows]


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        _pretty(obj)


def _pretty(obj, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                _pretty(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict):
                print(f"{pad}- " + ", ".join(f"{k}={x}" for k, x in v.items()))
            else:
                print(f"{pad}- {v}")


async def amain(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="muse-admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    sub.add_parser("users")
    sub.add_parser("encrypt-stream")
    sub.add_parser("decrypt-stream")
    f = sub.add_parser("forget-user")
    f.add_argument("user_id")
    f.add_argument("--yes", action="store_true")
    e = sub.add_parser("export-user")
    e.add_argument("user_id")
    sub.add_parser("reseal")
    r = sub.add_parser("rebuild")
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "encrypt-stream":
        get_vault().encrypt_stream(sys.stdin.buffer, sys.stdout.buffer)
        return 0
    if a.cmd == "decrypt-stream":
        get_vault().decrypt_stream(sys.stdin.buffer, sys.stdout.buffer)
        return 0

    for attempt in range(30):  # the database may still be starting
        try:
            conn = await connect()
            break
        except (OSError, asyncpg.CannotConnectNowError):
            await asyncio.sleep(2)
    else:
        print("database not reachable", file=sys.stderr)
        return 1
    try:
        if a.cmd == "migrate":
            _print(await migrate(conn), as_json=False)
        elif a.cmd == "status":
            _print(await collect(conn), a.json)
        elif a.cmd == "users":
            _print(await users(conn), as_json=False)
        elif a.cmd == "forget-user":
            if not a.yes:
                print("refusing without --yes (this permanently deletes everything about this person)", file=sys.stderr)
                return 2
            _print(await forget_user(conn, a.user_id), as_json=False)
        elif a.cmd == "export-user":
            print(json.dumps(await export_user(conn, a.user_id), indent=2, default=str, ensure_ascii=False))
        elif a.cmd == "reseal":
            _print(await reseal(conn), as_json=False)
        elif a.cmd == "rebuild":
            if not a.yes:
                print("refusing without --yes (drops catalog, profiles and vectors; they are rebuilt from events)", file=sys.stderr)
                return 2
            await rebuild(conn)
            print("derived state dropped; the worker rebuilds it on its next cycles")
    finally:
        await conn.close()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
