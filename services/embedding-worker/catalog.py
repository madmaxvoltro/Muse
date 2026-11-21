"""Item catalog maintenance: event log -> `item` rows -> `canon_item` rows (one per real title).

* New events since the watermark upsert their item (metadata merged, latest non-empty wins).
* Episodes get a parent: the Jellyfin series item, or a placeholder series item the enricher
  can resolve to a TMDB id (Netflix "Dark: Season 1: ..." -> a series called "Dark").
* Canonical rows are rebuilt for every canon key whose items changed: document text, tags,
  related titles, availability, library references.

Encrypted at rest (muse_schema.crypto): keys in queryable columns are blind indexes, and
metadata/enrichment/LLM profiles/canonical data are sealed. Real identifiers live inside the
sealed metadata as `_sid` (the source's own item id) and `_canon` (the real canonical key).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg
from muse_schema.crypto import P_EVENT_METADATA, P_ITEM_DATA, P_ITEM_KEY, get_vault, item_key
from muse_taste.canon import canon_key, domain_of, parent_canon_key
from muse_taste.documents import build_document, build_tags, merged_fields
from muse_taste.store import blind_canon, payload_for, payload_hash, seal_canon_data

logger = logging.getLogger("muse.worker.catalog")

# Keys that describe one event, not the item: never merged into item metadata.
EVENT_ONLY_KEYS = {"dedupe_key", "value", "via", "reason", "question_id", "imported", "query_id", "canon_key", "response", "members", "subkind"}
LIBRARY_SOURCES = {"jellyfin"}


def _clean(md: dict | None) -> dict:
    return {k: v for k, v in (md or {}).items() if k not in EVENT_ONLY_KEYS and v not in (None, "", [], {})}


def _strength(key: str | None) -> int:
    if not key:
        return 0
    return 3 if key.startswith("tmdb:") else 2 if key.startswith(("imdb:", "tvdb:", "yt:", "ytchannel:", "track:", "book:")) else 1


def open_item(row: Any) -> dict[str, Any]:
    """Decrypts an item row's sealed columns."""
    v = get_vault()
    return {
        **dict(row),
        "metadata": v.open_json(row["metadata"], P_ITEM_DATA) or {},
        "enrichment": v.open_json(row["enrichment"], P_ITEM_DATA) if "enrichment" in row.keys() and row["enrichment"] else None,
        "llm_profile": v.open_json(row["llm_profile"], P_ITEM_DATA) if "llm_profile" in row.keys() and row["llm_profile"] else None,
    }


def seal(value: Any) -> Any:
    return get_vault().seal_json(value, P_ITEM_DATA) if value is not None else None


async def sync_items(conn: asyncpg.Connection, since: datetime) -> tuple[set[str], datetime]:
    """Upserts items mentioned by events ingested after `since`. Returns touched users and the
    new watermark."""
    vault = get_vault()
    users = await conn.fetch("SELECT user_id, max(ingested_at) AS last FROM event WHERE ingested_at > $1 GROUP BY user_id", since)
    touched = {str(u["user_id"]) for u in users}
    max_ts = max([since, *(u["last"] for u in users)])
    rows = await conn.fetch(
        """
        SELECT source, source_item_id, item_type, metadata
        FROM event
        WHERE ingested_at > $1 AND source_item_id IS NOT NULL AND ingested_at <= $2 AND source <> 'muse'
        ORDER BY ingested_at
        """,
        since,
        max_ts,
    )
    if not rows:
        return touched, max_ts

    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        md = vault.open_json(r["metadata"], P_EVENT_METADATA) or {}
        key = f"{r['source']}:{r['source_item_id']}"  # stored form: source + blind id = item_key
        e = merged.setdefault(key, {"source": r["source"], "item_type": r["item_type"], "metadata": {}})
        e["metadata"].update(_clean(md))
        e["item_type"] = r["item_type"]
        if md.get("_sid"):
            e["sid"] = md["_sid"]

    existing = {
        r["item_key"]: open_item(r)
        for r in await conn.fetch("SELECT item_key, canon_key, metadata FROM item WHERE item_key = ANY($1::text[])", list(merged))
    }

    parents: dict[str, dict[str, Any]] = {}
    for key, e in merged.items():
        old = existing.get(key)
        md = {**((old or {}).get("metadata") or {}), **e["metadata"]}
        real_sid = md.get("_sid")
        new_canon = canon_key(e["source"], real_sid, e["item_type"], md)
        old_canon = md.get("_canon")
        canon = old_canon if _strength(old_canon) > _strength(new_canon) else new_canon
        md["_canon"] = canon
        parent_key = None
        pc = parent_canon_key(e["source"], e["item_type"], md)
        if pc:
            if e["source"] in LIBRARY_SOURCES and md.get("series_id"):
                parent_key = item_key(vault, e["source"], str(md["series_id"]))  # the library's own series item
            else:
                parent_key = f"parent:{vault.bid(pc, P_ITEM_KEY)}"
                parents[parent_key] = {
                    "canon": pc,
                    "metadata": _clean(
                        {
                            "title": md.get("series_title") or md.get("series_name"),
                            "external_ids": md.get("series_external_ids") or md.get("series_provider_ids"),
                            "genres": md.get("series_genres"),
                            "_canon": pc,
                        }
                    ),
                }
        await conn.execute(
            """
            INSERT INTO item (item_key, source, source_item_id, item_type, canon_key, parent_key, metadata, available, describable, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, now())
            ON CONFLICT (item_key) DO UPDATE SET
                item_type = EXCLUDED.item_type,
                canon_key = EXCLUDED.canon_key,
                parent_key = COALESCE(EXCLUDED.parent_key, item.parent_key),
                metadata = EXCLUDED.metadata,
                available = item.available OR EXCLUDED.available,
                describable = item.describable OR EXCLUDED.describable,
                updated_at = now()
            """,
            key,
            e["source"],
            key.split(":", 1)[1],
            e["item_type"],
            blind_canon(canon),
            parent_key,
            seal(md),
            e["source"] in LIBRARY_SOURCES and e["item_type"] != "series_episode",
            bool(md.get("title")),
        )

    for pkey, p in parents.items():
        old = await conn.fetchrow("SELECT item_key, canon_key, metadata FROM item WHERE item_key = $1", pkey)
        md = {**((open_item(old)["metadata"] if old else None) or {}), **p["metadata"]}
        await conn.execute(
            """
            INSERT INTO item (item_key, source, source_item_id, item_type, canon_key, metadata, describable, updated_at)
            VALUES ($1, 'parent', $2, 'series', $3, $4::jsonb, $5, now())
            ON CONFLICT (item_key) DO UPDATE SET metadata = EXCLUDED.metadata, describable = item.describable OR EXCLUDED.describable
            """,
            pkey,
            pkey.split(":", 1)[1],
            blind_canon(md.get("_canon") or p["canon"]),
            seal(md),
            bool(md.get("title")),
        )
    logger.info("catalog: %d items upserted, %d series placeholders", len(merged), len(parents))
    return touched, max_ts


async def rebuild_canon(conn: asyncpg.Connection, model_name: str, limit: int = 5000) -> int:
    """Rebuilds canon_item rows whose underlying items changed since the last rebuild."""
    vault = get_vault()
    keys = [
        r["canon_key"]
        for r in await conn.fetch(
            """
            SELECT DISTINCT i.canon_key FROM item i
            LEFT JOIN canon_item c ON c.canon_key = i.canon_key
            WHERE i.item_type <> 'series_episode'
              AND (c.canon_key IS NULL OR i.updated_at > c.updated_at)
              AND (i.describable OR i.enrichment IS NOT NULL)  -- nothing to describe yet
            LIMIT $1
            """,
            limit,
        )
    ]
    if not keys:
        return 0
    rows = await conn.fetch(
        """
        SELECT item_key, source, source_item_id, item_type, canon_key, metadata, enrichment, llm_profile, available, first_seen
        FROM item WHERE canon_key = ANY($1::text[]) AND item_type <> 'series_episode'
        """,
        keys,
    )
    by_canon: dict[str, list[dict]] = {}
    for r in rows:
        by_canon.setdefault(r["canon_key"], []).append(open_item(r))

    built = 0
    for ck, items in by_canon.items():
        # Representative: library copy first (its metadata is what the user sees), then richest.
        items.sort(key=lambda r: (r["source"] not in LIBRARY_SOURCES, r["source"] == "tmdb", -len(r["metadata"] or {})))
        rep = items[0]
        md: dict = {}
        for r in reversed(items):
            md.update(r["metadata"] or {})
        md.update(rep["metadata"] or {})
        enrichment = next((r["enrichment"] for r in items if r["enrichment"]), None)
        llm = next((r["llm_profile"] for r in items if r["llm_profile"]), None)
        item_type = rep["item_type"]
        f = merged_fields(item_type, md, enrichment, llm)
        doc = build_document(f)
        if not doc.strip() or not f["title"]:
            continue
        related: list[str] = []
        if enrichment:
            for rel in (enrichment.get("recommendations") or [])[:20] + (enrichment.get("similar") or [])[:10]:
                if rel.get("tmdb_id"):
                    related.append(blind_canon(f"tmdb:{'tv' if rel.get('kind') == 'tv' else 'movie'}:{rel['tmdb_id']}"))
        avail = [r for r in items if r["available"]]
        coll = (f.get("collection") or {}).get("id") if isinstance(f.get("collection"), dict) else None
        real_keys = sorted({r["metadata"].get("_canon") for r in items if r["metadata"].get("_canon")}, key=_strength, reverse=True)
        data = {
            "real_key": real_keys[0] if real_keys else None,
            "title": f["title"],
            "year": f.get("year"),
            "tags": build_tags(f),
            "related": list(dict.fromkeys(related)),
            "collection_id": str(coll) if coll else None,
            "vote_average": _f((enrichment or {}).get("vote_average") or md.get("vote_average") or md.get("community_rating")),
            "vote_count": _f((enrichment or {}).get("vote_count") or md.get("vote_count")),
            "popularity": _f((enrichment or {}).get("popularity") or md.get("popularity")),
            "library_refs": [{"source": r["source"], "source_item_id": r["metadata"].get("_sid")} for r in avail if r["metadata"].get("_sid")],
            "document": doc,
        }
        row = {"canon_key": ck, "item_type": item_type, "domain": domain_of(item_type), "available": bool(avail),
               "added_at": min((r["first_seen"] for r in avail), default=None), "data": data}
        await conn.execute(
            """
            INSERT INTO canon_item (canon_key, item_type, domain, available, added_at, data, doc_hash, payload_hash, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, now())
            ON CONFLICT (canon_key) DO UPDATE SET
                item_type = EXCLUDED.item_type, domain = EXCLUDED.domain, available = EXCLUDED.available,
                added_at = COALESCE(canon_item.added_at, EXCLUDED.added_at), data = EXCLUDED.data,
                doc_hash = EXCLUDED.doc_hash, payload_hash = EXCLUDED.payload_hash, updated_at = now()
            """,
            ck, row["item_type"], row["domain"], row["available"], row["added_at"], seal_canon_data(data),
            vault.digest(f"{model_name}\x00{doc}", "canon.doc_hash"), payload_hash(payload_for(row)),
        )
        built += 1
    return built


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def orphaned_canon_keys(conn: asyncpg.Connection) -> list[str]:
    """canon_item rows no item points at any more (an imdb: key upgraded to tmdb:)."""
    rows = await conn.fetch(
        "SELECT c.canon_key FROM canon_item c WHERE NOT EXISTS (SELECT 1 FROM item i WHERE i.canon_key = c.canon_key) LIMIT 1000"
    )
    return [r["canon_key"] for r in rows]
