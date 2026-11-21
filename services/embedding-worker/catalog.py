"""Item catalog maintenance: event log -> `item` rows -> `canon_item` rows (one per real title).

* New events since the watermark upsert their item (metadata merged, latest non-empty wins).
* Episodes get a parent: the Jellyfin series item, or a placeholder series item the enricher
  can resolve to a TMDB id (Netflix "Dark: Season 1: ..." -> a series called "Dark").
* Canonical rows are rebuilt for every canon key whose items changed: document text, tags,
  related titles, availability, library references.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg
from muse_taste.canon import canon_key, domain_of, parent_canon_key
from muse_taste.documents import build_document, build_tags, document_hash, merged_fields
from muse_taste.store import payload_for, payload_hash

logger = logging.getLogger("muse.worker.catalog")

# Keys that describe one event, not the item: never merged into item metadata.
EVENT_ONLY_KEYS = {"dedupe_key", "value", "via", "reason", "question_id", "imported", "query_id", "canon_key"}
LIBRARY_SOURCES = {"jellyfin"}


def _clean(md: dict | None) -> dict:
    return {k: v for k, v in (md or {}).items() if k not in EVENT_ONLY_KEYS and v not in (None, "", [], {})}


def _strength(key: str) -> int:
    return 3 if key.startswith("tmdb:") else 2 if key.startswith(("imdb:", "tvdb:", "yt:", "ytchannel:", "track:", "book:")) else 1


async def sync_items(conn: asyncpg.Connection, since: datetime) -> tuple[set[str], datetime]:
    """Upserts items mentioned by events ingested after `since`. Returns touched users and the
    new watermark."""
    users = await conn.fetch("SELECT user_id, max(ingested_at) AS last FROM event WHERE ingested_at > $1 GROUP BY user_id", since)
    touched = {str(u["user_id"]) for u in users}
    max_ts = max([since, *(u["last"] for u in users)])
    rows = await conn.fetch(
        """
        SELECT source, source_item_id, item_type, metadata
        FROM event
        WHERE ingested_at > $1 AND source_item_id IS NOT NULL AND ingested_at <= $2
        ORDER BY ingested_at
        """,
        since,
        max_ts,
    )
    if not rows:
        return touched, max_ts

    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r['source']}:{r['source_item_id']}"
        e = merged.setdefault(key, {"source": r["source"], "sid": r["source_item_id"], "item_type": r["item_type"], "metadata": {}, "event_canon": None})
        md = r["metadata"] or {}
        e["metadata"].update(_clean(md))
        e["item_type"] = r["item_type"]
        if md.get("canon_key"):
            e["event_canon"] = md["canon_key"]

    existing = {
        r["item_key"]: r
        for r in await conn.fetch("SELECT item_key, canon_key, metadata FROM item WHERE item_key = ANY($1::text[])", list(merged))
    }

    parents: dict[str, dict[str, Any]] = {}
    for key, e in merged.items():
        old = existing.get(key)
        md = {**((old["metadata"] if old else None) or {}), **e["metadata"]}
        if e["source"] == "muse":
            continue  # answers reference canonical titles that already exist; no catalog row
        new_canon = e["event_canon"] or canon_key(e["source"], e["sid"], e["item_type"], md)
        canon = old["canon_key"] if old and _strength(old["canon_key"]) > _strength(new_canon) else new_canon
        parent_key = None
        pc = parent_canon_key(e["source"], e["item_type"], md)
        if pc:
            if e["source"] in LIBRARY_SOURCES and md.get("series_id"):
                parent_key = f"{e['source']}:{md['series_id']}"  # the library's own series item
            else:
                parent_key = f"parent:{pc}"
                parents[parent_key] = {
                    "canon": pc if not pc.startswith("title:") else parent_key,
                    "metadata": _clean(
                        {
                            "title": md.get("series_title") or md.get("series_name"),
                            "external_ids": md.get("series_external_ids") or md.get("series_provider_ids"),
                            "genres": md.get("series_genres"),
                        }
                    ),
                }
        await conn.execute(
            """
            INSERT INTO item (item_key, source, source_item_id, item_type, canon_key, parent_key, metadata, available, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, now())
            ON CONFLICT (item_key) DO UPDATE SET
                item_type = EXCLUDED.item_type,
                canon_key = EXCLUDED.canon_key,
                parent_key = COALESCE(EXCLUDED.parent_key, item.parent_key),
                metadata = item.metadata || EXCLUDED.metadata,
                available = item.available OR EXCLUDED.available,
                updated_at = now()
            """,
            key,
            e["source"],
            e["sid"],
            e["item_type"],
            canon,
            parent_key,
            md,  # the pool codec encodes jsonb
            e["source"] in LIBRARY_SOURCES and e["item_type"] != "series_episode",
        )

    for pkey, p in parents.items():
        await conn.execute(
            """
            INSERT INTO item (item_key, source, source_item_id, item_type, canon_key, metadata, updated_at)
            VALUES ($1, 'parent', $2, 'series', $3, $4::jsonb, now())
            ON CONFLICT (item_key) DO UPDATE SET metadata = item.metadata || EXCLUDED.metadata
            """,
            pkey,
            pkey.split(":", 1)[1],
            p["canon"],
            p["metadata"],
        )
    logger.info("catalog: %d items upserted, %d series placeholders", len(merged), len(parents))
    return touched, max_ts


async def rebuild_canon(conn: asyncpg.Connection, model_name: str, limit: int = 5000) -> int:
    """Rebuilds canon_item rows whose underlying items changed since the last rebuild."""
    keys = [
        r["canon_key"]
        for r in await conn.fetch(
            """
            SELECT DISTINCT i.canon_key FROM item i
            LEFT JOIN canon_item c ON c.canon_key = i.canon_key
            WHERE i.item_type <> 'series_episode'
              AND (c.canon_key IS NULL OR i.updated_at > c.updated_at)
              AND (i.metadata ? 'title' OR i.enrichment IS NOT NULL)  -- nothing to describe yet
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
    by_canon: dict[str, list] = {}
    for r in rows:
        by_canon.setdefault(r["canon_key"], []).append(r)

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
        tags = build_tags(f)
        related: list[str] = []
        if enrichment:
            for rel in (enrichment.get("recommendations") or [])[:20] + (enrichment.get("similar") or [])[:10]:
                if rel.get("tmdb_id"):
                    related.append(f"tmdb:{'tv' if rel.get('kind') == 'tv' else 'movie'}:{rel['tmdb_id']}")
        avail = [r for r in items if r["available"]]
        coll = (f.get("collection") or {}).get("id") if isinstance(f.get("collection"), dict) else None
        row = {
            "canon_key": ck,
            "item_type": item_type,
            "domain": domain_of(item_type),
            "title": f["title"],
            "year": f.get("year"),
            "tags": tags,
            "related": list(dict.fromkeys(related)),
            "collection_id": str(coll) if coll else None,
            "vote_average": (enrichment or {}).get("vote_average") or md.get("vote_average") or md.get("community_rating"),
            "vote_count": (enrichment or {}).get("vote_count") or md.get("vote_count"),
            "popularity": (enrichment or {}).get("popularity") or md.get("popularity"),
            "available": bool(avail),
            "library_refs": [{"source": r["source"], "source_item_id": r["source_item_id"]} for r in avail],
            "added_at": min((r["first_seen"] for r in avail), default=None),
        }
        pl = payload_for(row)
        await conn.execute(
            """
            INSERT INTO canon_item (canon_key, item_type, domain, title, year, tags, related, collection_id, vote_average,
                                    vote_count, popularity, available, library_refs, added_at, document, doc_hash, payload_hash, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15,$16,$17, now())
            ON CONFLICT (canon_key) DO UPDATE SET
                item_type=EXCLUDED.item_type, domain=EXCLUDED.domain, title=EXCLUDED.title, year=EXCLUDED.year,
                tags=EXCLUDED.tags, related=EXCLUDED.related, collection_id=EXCLUDED.collection_id,
                vote_average=EXCLUDED.vote_average, vote_count=EXCLUDED.vote_count, popularity=EXCLUDED.popularity,
                available=EXCLUDED.available, library_refs=EXCLUDED.library_refs,
                added_at=COALESCE(canon_item.added_at, EXCLUDED.added_at), document=EXCLUDED.document,
                doc_hash=EXCLUDED.doc_hash, payload_hash=EXCLUDED.payload_hash, updated_at=now()
            """,
            ck, row["item_type"], row["domain"], row["title"], row["year"], row["tags"], row["related"],
            row["collection_id"], _f(row["vote_average"]), _f(row["vote_count"]), _f(row["popularity"]), row["available"],
            row["library_refs"], row["added_at"], doc, document_hash(doc, model_name), payload_hash(pl),
        )
    return len(by_canon)


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
