"""Operational status, shared by `muse status` (admin tool) and the status page. Counts and
timestamps only: nothing here identifies a person or a title."""

from __future__ import annotations

from typing import Any


async def collect(conn: Any) -> dict[str, Any]:
    async def val(sql: str, default: Any = None) -> Any:
        try:
            return await conn.fetchval(sql)
        except Exception:
            return default

    st = None
    try:
        st = await conn.fetchrow("SELECT * FROM embedding_worker_state")
    except Exception:
        pass
    st = dict(st) if st else {}
    report = st.get("last_cycle_report")
    if isinstance(report, str):
        import json

        report = json.loads(report)
    return {
        "worker": {
            "model": st.get("model_name"),
            "vector_dim": st.get("vector_dim"),
            "last_cycle_at": _iso(st.get("last_cycle_at")),
            "last_error_at": _iso(st.get("last_error_at")),
            "last_events_processed_until": _iso(st.get("last_processed_at")),
            "last_full_profile_refresh": _iso(st.get("last_full_profile_at")),
            "last_cycle": {k: v for k, v in (report or {}).items() if isinstance(v, (int, float, str, bool)) or v is None},
        },
        "events": {
            "total": await val("SELECT count(*) FROM event", 0),
            "last_24h": await val("SELECT count(*) FROM event WHERE ingested_at > now() - interval '24 hours'", 0),
        },
        "users": await val("SELECT count(DISTINCT user_id) FROM event WHERE action <> 'cataloged'", 0),
        "catalog": {
            "titles": await val("SELECT count(*) FROM canon_item", 0),
            "in_library": await val("SELECT count(*) FROM canon_item WHERE available", 0),
            "embedded": await val("SELECT count(*) FROM canon_item WHERE doc_hash IS NOT NULL AND embedded_hash = doc_hash", 0),
            "waiting_for_embedding": await val("SELECT count(*) FROM canon_item WHERE doc_hash IS NOT NULL AND embedded_hash IS DISTINCT FROM doc_hash", 0),
            "waiting_for_tmdb": await val("SELECT count(*) FROM item WHERE source <> 'tmdb' AND item_type IN ('movie','series') AND enriched_at IS NULL", 0),
        },
        "profiles": await val("SELECT count(*) FROM taste_profile", 0),
        "questions": {
            "pending": await val("SELECT count(*) FROM question_log WHERE status = 'pending'", 0),
            "answered_7d": await val("SELECT count(*) FROM question_log WHERE answered_at > now() - interval '7 days'", 0),
        },
        "migrations": [r["version"] for r in await _rows(conn, "SELECT version FROM schema_migrations ORDER BY version")],
    }


async def _rows(conn: Any, sql: str) -> list:
    try:
        return await conn.fetch(sql)
    except Exception:
        return []


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else v
