"""Jellyfin adapter — a poller, not a proxy (unlike the SearxNG adapter): Jellyfin
already exposes a full REST API, so there's no need to sit in front of traffic.

Two jobs on independent intervals:

1. Catalog sync (slow, default daily): walks the whole library via
   `/Users/{id}/Items`, emits one `cataloged` event per item (action_weight=0,
   see shared/muse_schema/event.py) so the embedding worker can build content
   embeddings for the existing library from day one — this is what lets Muse
   start recommending against a library that already exists, before any new
   watch history has accumulated.
2. Watch-state sync (fast, default every 5 min): diffs each item's `UserData`
   (Played, PlaybackPositionTicks, IsFavorite) against what was seen last poll,
   emits `played` / `completed` / `favorited` events on change.

State is kept in memory only (no local DB access — adapters are write-only
into the ingestion API by design, see docs/architecture.md Security model).
This means a container restart re-diffs from a blank slate and may re-emit a
few duplicate events; harmless for `cataloged` (idempotent upsert) and a minor,
accepted inaccuracy for watch-state (an extra `completed` event now and then).

Identity: Jellyfin already assigns every user a stable GUID, so this adapter
uses that GUID directly as the Muse `user_id` — it's the anchor other sources'
usernames get fuzzy-matched against later (see docs/architecture.md Multi-user),
not itself a fuzzy match.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.jellyfin_adapter")

JELLYFIN_URL = os.environ["JELLYFIN_URL"].rstrip("/")
JELLYFIN_API_KEY = os.environ["JELLYFIN_API_KEY"]
INGESTION_URL = os.environ["MUSE_INGESTION_URL"]
ADAPTER_KEY = os.environ["MUSE_ADAPTER_KEY"]

CATALOG_SYNC_INTERVAL_SECONDS = int(os.environ.get("MUSE_CATALOG_SYNC_INTERVAL_SECONDS", "86400"))
WATCH_POLL_INTERVAL_SECONDS = int(os.environ.get("MUSE_WATCH_POLL_INTERVAL_SECONDS", "300"))

JELLYFIN_TYPE_MAP = {
    "Movie": "movie",
    "Episode": "series_episode",
    "Audio": "track",
    "AudioBook": "audiobook",
    "Book": "ebook",
}

_headers = {"X-Emby-Token": JELLYFIN_API_KEY}


async def jellyfin_get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
    resp = await client.get(f"{JELLYFIN_URL}{path}", headers=_headers, params=params or {})
    resp.raise_for_status()
    return resp.json()


async def list_users(client: httpx.AsyncClient) -> list[dict]:
    return await jellyfin_get(client, "/Users")


async def list_items(client: httpx.AsyncClient, user_id: str) -> list[dict]:
    data = await jellyfin_get(
        client,
        f"/Users/{user_id}/Items",
        params={
            "Recursive": "true",
            "IncludeItemTypes": ",".join(JELLYFIN_TYPE_MAP),
            "Fields": "Genres,Overview,ProductionYear,RunTimeTicks",
        },
    )
    return data.get("Items", [])


async def post_event(client: httpx.AsyncClient, event: dict) -> None:
    try:
        resp = await client.post(
            f"{INGESTION_URL}/events", json=event, headers={"X-Muse-Api-Key": ADAPTER_KEY}
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("failed to post event action=%s item=%s", event.get("action"), event.get("source_item_id"))


def item_metadata(item: dict) -> dict:
    return {
        "title": item.get("Name", ""),
        "description": item.get("Overview", "") or "",
        "genres": item.get("Genres", []) or [],
        "year": item.get("ProductionYear"),
    }


async def sync_catalog(client: httpx.AsyncClient, users: list[dict]) -> None:
    logger.info("starting catalog sync for %d users", len(users))
    seen_items: set[str] = set()
    for user in users:
        items = await list_items(client, user["Id"])
        for item in items:
            item_type = JELLYFIN_TYPE_MAP.get(item.get("Type"))
            if item_type is None or item["Id"] in seen_items:
                continue
            seen_items.add(item["Id"])
            await post_event(
                client,
                {
                    "user_id": user["Id"],
                    "source": "jellyfin",
                    "source_item_id": item["Id"],
                    "item_type": item_type,
                    "action": "cataloged",
                    "action_weight": 0.0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": item_metadata(item),
                },
            )
    logger.info("catalog sync done, %d distinct items", len(seen_items))


def _watch_state(item: dict) -> tuple[bool, float, bool]:
    user_data = item.get("UserData", {}) or {}
    played = bool(user_data.get("Played", False))
    runtime_ticks = item.get("RunTimeTicks") or 0
    position_ticks = user_data.get("PlaybackPositionTicks", 0) or 0
    progress_pct = (position_ticks / runtime_ticks * 100) if runtime_ticks else 0.0
    favorite = bool(user_data.get("IsFavorite", False))
    return played, progress_pct, favorite


async def sync_watch_state(
    client: httpx.AsyncClient, users: list[dict], last_state: dict[tuple[str, str], tuple[bool, float, bool]]
) -> None:
    for user in users:
        items = await list_items(client, user["Id"])
        for item in items:
            item_type = JELLYFIN_TYPE_MAP.get(item.get("Type"))
            if item_type is None:
                continue

            key = (user["Id"], item["Id"])
            played, progress_pct, favorite = _watch_state(item)
            prev = last_state.get(key)
            last_state[key] = (played, progress_pct, favorite)

            if prev is None:
                # First time seeing this (user, item) pair this run — nothing changed yet,
                # nothing to emit (catalog sync already recorded the item existing).
                continue

            prev_played, prev_progress, prev_favorite = prev
            now = datetime.now(timezone.utc).isoformat()
            base = {
                "user_id": user["Id"],
                "source": "jellyfin",
                "source_item_id": item["Id"],
                "item_type": item_type,
                "timestamp": now,
                "progress_pct": progress_pct,
                "metadata": item_metadata(item),
            }

            if played and not prev_played:
                await post_event(client, {**base, "action": "completed"})
            elif progress_pct > prev_progress + 1.0 and not played:
                await post_event(client, {**base, "action": "played"})

            if favorite and not prev_favorite:
                await post_event(client, {**base, "action": "favorited"})


async def main() -> None:
    last_catalog_sync = float("-inf")  # force a catalog sync on first startup
    watch_state: dict[tuple[str, str], tuple[bool, float, bool]] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.info("jellyfin adapter started, watching %s", JELLYFIN_URL)
        while True:
            start = time.monotonic()
            try:
                users = await list_users(client)

                if start - last_catalog_sync >= CATALOG_SYNC_INTERVAL_SECONDS:
                    await sync_catalog(client, users)
                    last_catalog_sync = start

                await sync_watch_state(client, users, watch_state)
            except Exception:
                logger.exception("jellyfin adapter cycle failed")

            elapsed = time.monotonic() - start
            await asyncio.sleep(max(WATCH_POLL_INTERVAL_SECONDS - elapsed, 5))


if __name__ == "__main__":
    asyncio.run(main())
