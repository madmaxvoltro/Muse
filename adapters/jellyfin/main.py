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
   (Played, PlaybackPositionTicks, IsFavorite, Rating) against what was seen last
   poll, emits `played` / `completed` / `favorited` / `rated` events on change. The
   first sighting of each (user, item) imports the existing history once, with
   `metadata.dedupe_key` so restarts don't double count.

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
    "Series": "series",  # thumbs and favourites on a show are stored on the series
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
            # ProviderIds give canonical identity (TMDB/IMDb), People/Tags/Studios feed the
            # taste engine's exact-match tags, SeriesId rolls episodes up to their show.
            "Fields": "Genres,Overview,ProductionYear,RunTimeTicks,ProviderIds,People,Tags,Studios,Taglines,OfficialRating,CommunityRating,DateCreated",
        },
    )
    return data.get("Items", [])


async def post_event(client: httpx.AsyncClient, event: dict) -> bool:
    try:
        resp = await client.post(
            f"{INGESTION_URL}/events", json=event, headers={"X-Muse-Api-Key": ADAPTER_KEY}
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("failed to post event action=%s item=%s: %s", event.get("action"), event.get("source_item_id"), exc)
        return False


def item_metadata(item: dict) -> dict:
    md = {
        "title": item.get("Name", ""),
        "description": item.get("Overview", "") or "",
        "genres": item.get("Genres", []) or [],
        "year": item.get("ProductionYear"),
        "provider_ids": {k: v for k, v in (item.get("ProviderIds") or {}).items() if v},
        "people": [
            {"name": p.get("Name"), "type": p.get("Type")}
            for p in (item.get("People") or [])[:15]
            if p.get("Name") and p.get("Type") in ("Actor", "Director", "Writer", "Producer")
        ],
        "tags": (item.get("Tags") or [])[:20],
        "studios": [s.get("Name") for s in (item.get("Studios") or []) if s.get("Name")][:4],
        "taglines": (item.get("Taglines") or [])[:1],
        "official_rating": item.get("OfficialRating"),
        "community_rating": item.get("CommunityRating"),
        "runtime_min": round(item["RunTimeTicks"] / 600_000_000) if item.get("RunTimeTicks") else None,
    }
    if item.get("Type") == "Episode":
        md["series_id"] = item.get("SeriesId")
        md["series_name"] = item.get("SeriesName")
        md["season"] = item.get("ParentIndexNumber")
        md["episode"] = item.get("IndexNumber")
    if item.get("Type") == "Audio":
        md["artist"] = (item.get("AlbumArtist") or (item.get("Artists") or [None])[0])
        md["album"] = item.get("Album")
    return {k: v for k, v in md.items() if v not in (None, "", [], {})} | {"title": md["title"]}


async def sync_catalog(client: httpx.AsyncClient, users: list[dict]) -> bool:
    """Returns False if any event failed, so the caller retries the sync next cycle."""
    logger.info("starting catalog sync for %d users", len(users))
    seen_items: set[str] = set()
    ok = True
    for user in users:
        items = await list_items(client, user["Id"])
        for item in items:
            item_type = JELLYFIN_TYPE_MAP.get(item.get("Type"))
            if item_type is None or item["Id"] in seen_items:
                continue
            seen_items.add(item["Id"])
            ok &= await post_event(
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
    logger.info("catalog sync done, %d distinct items%s", len(seen_items), "" if ok else " (some failed, will retry)")
    return ok


# Vesper writes its thumbs as the Jellyfin user rating: 10 love, 8 up, 1 down. Anything else on
# the 0-10 scale is read with Jellyfin's own 6.5 like/dislike split.
def _thumbs(rating: float | None) -> int:
    if rating is None:
        return 0
    if rating >= 9.5:
        return 2
    if rating >= 6.5:
        return 1
    return -1


RATED_WEIGHTS = {-1: -1.0, 0: 0.0, 1: 1.0, 2: 2.0}  # mirrors muse_schema.RATED_WEIGHTS


def _watch_state(item: dict) -> tuple[bool, float, bool, int]:
    user_data = item.get("UserData", {}) or {}
    played = bool(user_data.get("Played", False))
    runtime_ticks = item.get("RunTimeTicks") or 0
    position_ticks = user_data.get("PlaybackPositionTicks", 0) or 0
    progress_pct = (position_ticks / runtime_ticks * 100) if runtime_ticks else 0.0
    favorite = bool(user_data.get("IsFavorite", False))
    return played, progress_pct, favorite, _thumbs(user_data.get("Rating"))


async def sync_watch_state(
    client: httpx.AsyncClient, users: list[dict], last_state: dict[tuple[str, str], tuple[bool, float, bool, int]]
) -> None:
    for user in users:
        items = await list_items(client, user["Id"])
        for item in items:
            item_type = JELLYFIN_TYPE_MAP.get(item.get("Type"))
            if item_type is None:
                continue

            key = (user["Id"], item["Id"])
            played, progress_pct, favorite, thumbs = _watch_state(item)
            prev = last_state.get(key)
            events: list[dict] = []

            now = datetime.now(timezone.utc).isoformat()
            last_played = (item.get("UserData") or {}).get("LastPlayedDate") or ""
            base = {
                "user_id": user["Id"],
                "source": "jellyfin",
                "source_item_id": item["Id"],
                "item_type": item_type,
                "timestamp": now,
                "progress_pct": progress_pct,
                "metadata": item_metadata(item),
            }

            def keyed(event: dict, key: str) -> dict:
                # metadata.dedupe_key makes the ingestion API drop repeats (restarts, history import).
                return {**event, "metadata": {**event["metadata"], "dedupe_key": key}}

            if prev is None:
                # First sighting this run: import the existing history once. Keys make this
                # idempotent across restarts; timestamps are when it actually happened.
                hist = {**base, "timestamp": last_played or now}
                if played:
                    events.append(keyed({**hist, "action": "completed"}, f"completed:{item['Id']}:{last_played}"))
                elif progress_pct > 1.0:
                    events.append(keyed({**hist, "action": "played"}, f"played:{item['Id']}:{last_played}"))
                if favorite:
                    events.append(keyed({**base, "action": "favorited"}, f"favorited:{item['Id']}"))
                if thumbs != 0:
                    events.append(
                        keyed(
                            {**base, "action": "rated", "action_weight": RATED_WEIGHTS[thumbs], "metadata": {**base["metadata"], "value": thumbs}},
                            f"rated:{item['Id']}:{thumbs}",
                        ),
                    )

            else:
                prev_played, prev_progress, prev_favorite, prev_thumbs = prev

                if played and not prev_played:
                    events.append(keyed({**base, "action": "completed"}, f"completed:{item['Id']}:{last_played}"))
                elif progress_pct > prev_progress + 1.0 and not played:
                    events.append({**base, "action": "played"})

                if favorite and not prev_favorite:
                    events.append(keyed({**base, "action": "favorited"}, f"favorited:{item['Id']}"))

                if thumbs != prev_thumbs:
                    events.append(
                        {
                            **base,
                            "action": "rated",
                            "action_weight": RATED_WEIGHTS[thumbs],
                            "metadata": {**base["metadata"], "value": thumbs},
                        },
                    )

            # Remember the state only once Muse has it; on failure the next poll tries again
            # (history import events are deduplicated, so a retry never double counts).
            ok = True
            for event in events:
                ok = await post_event(client, event) and ok
            if ok:
                last_state[key] = (played, progress_pct, favorite, thumbs)


async def main() -> None:
    last_catalog_sync = float("-inf")  # force a catalog sync on first startup
    watch_state: dict[tuple[str, str], tuple[bool, float, bool, int]] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.info("jellyfin adapter started, watching %s", JELLYFIN_URL)
        while True:
            start = time.monotonic()
            try:
                users = await list_users(client)

                if start - last_catalog_sync >= CATALOG_SYNC_INTERVAL_SECONDS:
                    if await sync_catalog(client, users):
                        last_catalog_sync = start

                await sync_watch_state(client, users, watch_state)
            except Exception:
                logger.exception("jellyfin adapter cycle failed")

            elapsed = time.monotonic() - start
            await asyncio.sleep(max(WATCH_POLL_INTERVAL_SECONDS - elapsed, 5))


if __name__ == "__main__":
    asyncio.run(main())
