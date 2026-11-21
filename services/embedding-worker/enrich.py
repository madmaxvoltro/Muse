"""Enrichment: TMDB metadata for films/series (and the titles TMDB recommends alongside them),
then optional LLM tone/theme descriptors. Both are budgeted per cycle and cached forever-ish
(TMDB refreshed monthly), so a big library fills in over a few cycles without hammering
anything."""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg
from muse_taste import llm as llm_mod
from muse_taste.canon import external_ids
from muse_taste.documents import merged_fields
from muse_taste.tmdb import TMDBClient

logger = logging.getLogger("muse.worker.enrich")

TMDB_ITEMS_PER_CYCLE = int(os.environ.get("MUSE_TMDB_ITEMS_PER_CYCLE", "150"))
TMDB_REFRESH_DAYS = int(os.environ.get("MUSE_TMDB_REFRESH_DAYS", "30"))
RELATED_PER_ITEM = int(os.environ.get("MUSE_RELATED_PER_ITEM", "12"))
LLM_ITEMS_PER_CYCLE = int(os.environ.get("MUSE_LLM_ITEMS_PER_CYCLE", "40"))


async def enrich_tmdb(pool: asyncpg.Pool, tmdb: TMDBClient) -> int:
    async with pool.acquire() as conn:
        todo = await conn.fetch(
            """
            SELECT item_key, source, item_type, metadata, canon_key FROM item
            WHERE source <> 'tmdb' AND item_type IN ('movie', 'series')
              AND enrich_failures < 3
              AND (enriched_at IS NULL OR enriched_at < now() - make_interval(days => $1::int))
            ORDER BY available DESC, enriched_at NULLS FIRST, first_seen
            LIMIT $2
            """,
            TMDB_REFRESH_DAYS,
            TMDB_ITEMS_PER_CYCLE,
        )
    done = 0
    for row in todo:
        kind = "movie" if row["item_type"] == "movie" else "tv"
        md = row["metadata"] or {}
        try:
            tmdb_id = await _resolve(tmdb, kind, md, row["canon_key"])
            details = await tmdb.details(kind, tmdb_id) if tmdb_id else None
        except Exception as exc:  # network / API errors: count and move on
            logger.warning("tmdb lookup failed for %s: %s", row["item_key"], exc)
            details = None
        async with pool.acquire() as conn:
            if not details:
                await conn.execute(
                    "UPDATE item SET enrich_failures = enrich_failures + 1, enriched_at = now() WHERE item_key = $1", row["item_key"]
                )
                continue
            new_canon = f"tmdb:{kind}:{details['tmdb_id']}"
            await conn.execute(
                "UPDATE item SET enrichment = $2::jsonb, enriched_at = now(), enrich_failures = 0, canon_key = $3, updated_at = now() WHERE item_key = $1",
                row["item_key"],
                details,
                new_canon,
            )
            await _upsert_related(conn, details)
        done += 1
    if done:
        logger.info("tmdb: enriched %d/%d items", done, len(todo))
    return done


async def _resolve(tmdb: TMDBClient, kind: str, md: dict, canon: str) -> int | None:
    if canon.startswith(f"tmdb:{kind}:"):
        return int(canon.rsplit(":", 1)[1])
    ids = external_ids(md)
    if ids.get("tmdb"):
        return int(ids["tmdb"])
    if ids.get("imdb"):
        found = await tmdb.find(ids["imdb"], "imdb_id")
        if found and found[0] == kind:
            return found[1]
    if ids.get("tvdb") and kind == "tv":
        found = await tmdb.find(ids["tvdb"], "tvdb_id")
        if found:
            return found[1]
    title = md.get("title")
    if title:
        year = md.get("year")
        return await tmdb.search(kind, str(title), int(year) if isinstance(year, (int, float)) or str(year).isdigit() else None)
    return None


async def _upsert_related(conn: asyncpg.Connection, details: dict) -> None:
    """TMDB's recommendations become catalog items (not in the library) so they can be embedded
    and scored: the source of 'fans of X also liked Y' candidates and of discovery for the
    curator."""
    for rel in (details.get("recommendations") or [])[:RELATED_PER_ITEM]:
        if not rel.get("tmdb_id") or not rel.get("title"):
            continue
        kind = "tv" if rel.get("kind") == "tv" else "movie"
        md = {
            "title": rel["title"],
            "description": rel.get("overview") or "",
            "genres": rel.get("genres") or [],
            "year": rel.get("year"),
            "vote_average": rel.get("vote_average"),
            "vote_count": rel.get("vote_count"),
            "popularity": rel.get("popularity"),
            "language": rel.get("original_language"),
            "external_ids": {"tmdb": str(rel["tmdb_id"])},
        }
        await conn.execute(
            """
            INSERT INTO item (item_key, source, source_item_id, item_type, canon_key, metadata, available)
            VALUES ($1, 'tmdb', $2, $3, $4, $5::jsonb, FALSE)
            ON CONFLICT (item_key) DO NOTHING
            """,
            f"tmdb:{kind}:{rel['tmdb_id']}",
            f"{kind}:{rel['tmdb_id']}",
            "movie" if kind == "movie" else "series",
            f"tmdb:{kind}:{rel['tmdb_id']}",
            md,  # the pool codec encodes jsonb
        )


async def describe_items(pool: asyncpg.Pool, llm: llm_mod.LLM) -> int:
    """LLM tone/themes for library titles (and anything you interacted with) first."""
    async with pool.acquire() as conn:
        todo = await conn.fetch(
            """
            SELECT DISTINCT ON (canon_key) item_key, item_type, metadata, enrichment, canon_key
            FROM item
            WHERE llm_at IS NULL AND item_type IN ('movie', 'series', 'youtube_video', 'audiobook', 'ebook')
              AND (source <> 'tmdb' OR available)
            ORDER BY canon_key, available DESC, enrichment IS NOT NULL DESC
            LIMIT $1
            """,
            LLM_ITEMS_PER_CYCLE,
        )
    done = 0
    for row in todo:
        fields = merged_fields(row["item_type"], row["metadata"] or {}, row["enrichment"])
        try:
            prof = await asyncio.to_thread(llm_mod.describe_item, llm, fields)
        except Exception as exc:
            logger.warning("llm describe failed for %s: %s", row["item_key"], exc)
            prof = None
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE item SET llm_profile = $2::jsonb, llm_at = now(), updated_at = CASE WHEN $2::jsonb IS NULL THEN updated_at ELSE now() END WHERE item_key = $1",
                row["item_key"],
                prof,
            )
        done += bool(prof)
    if todo:
        logger.info("llm: described %d/%d items", done, len(todo))
    return done
