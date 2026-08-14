"""arr-stack adapter — polls Radarr + Sonarr, diffs their library state against what
was seen last poll, and emits `added_watchlist` / `rejected` / `removed` events.

Deliberately unaware of *why* an item disappeared (curator-driven cleanup vs. a human
manually deleting it in Radarr/Sonarr's own UI) — see docs/architecture.md and
services/curator/README.md for why that's fine: adapters are write-only and never read
back from the event store (security model), so this adapter can't know the curator's
internal bookkeeping, and doesn't need to. A `removed` event's default weight (-0.3, see
shared/muse_schema/event.py) is mild enough that an extra one on an item the curator
already scored low for removal doesn't meaningfully change anything.

Distinguishes two removal cases by whether the item ever got a file:
- had a file, then disappeared -> `removed` (mild negative — consumed to some degree)
- never got a file, then disappeared -> `rejected` (stronger negative — added, never
  even fulfilled, then discarded; a much clearer "this was a bad add" signal)

No per-user attribution: Radarr/Sonarr have no concept of "who added this", so all
events use MUSE_DEFAULT_USER_ID, same as the SearxNG adapter's v1 fallback.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("muse.arr_adapter")

RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

INGESTION_URL = os.environ["MUSE_INGESTION_URL"]
ADAPTER_KEY = os.environ["MUSE_ADAPTER_KEY"]
DEFAULT_USER_ID = os.environ["MUSE_DEFAULT_USER_ID"]

POLL_INTERVAL_SECONDS = int(os.environ.get("MUSE_ARR_POLL_INTERVAL_SECONDS", "900"))


async def post_event(client: httpx.AsyncClient, event: dict) -> None:
    try:
        resp = await client.post(f"{INGESTION_URL}/events", json=event, headers={"X-Muse-Api-Key": ADAPTER_KEY})
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("failed to post event action=%s item=%s", event.get("action"), event.get("source_item_id"))


async def fetch_radarr_movies(client: httpx.AsyncClient) -> list[dict]:
    if not RADARR_URL:
        return []
    resp = await client.get(f"{RADARR_URL}/api/v3/movie", headers={"X-Api-Key": RADARR_API_KEY})
    resp.raise_for_status()
    return [
        {
            "item_type": "movie",
            "source_item_id": str(m["tmdbId"]),
            "has_file": bool(m.get("hasFile", False)),
            "title": m.get("title", ""),
            "overview": m.get("overview", "") or "",
            "genres": m.get("genres", []) or [],
            "quality_profile_id": m.get("qualityProfileId"),
            "added": m.get("added"),
        }
        for m in resp.json()
    ]


async def fetch_sonarr_series(client: httpx.AsyncClient) -> list[dict]:
    if not SONARR_URL:
        return []
    resp = await client.get(f"{SONARR_URL}/api/v3/series", headers={"X-Api-Key": SONARR_API_KEY})
    resp.raise_for_status()
    out = []
    for s in resp.json():
        stats = s.get("statistics", {}) or {}
        out.append(
            {
                "item_type": "series_episode",  # schema has no "series" item_type — see docs/adapters/arr.md
                "source_item_id": str(s["tvdbId"]),
                "has_file": (stats.get("episodeFileCount") or 0) > 0,
                "title": s.get("title", ""),
                "overview": s.get("overview", "") or "",
                "genres": s.get("genres", []) or [],
                "quality_profile_id": s.get("qualityProfileId"),
                "added": s.get("added"),
            }
        )
    return out


def item_metadata(item: dict) -> dict:
    return {
        "title": item["title"],
        "description": item["overview"],
        "genres": item["genres"],
        "quality_profile_id": item["quality_profile_id"],
    }


async def sync(client: httpx.AsyncClient, last_state: dict[tuple[str, str], dict]) -> None:
    items = (await fetch_radarr_movies(client)) + (await fetch_sonarr_series(client))
    seen_keys: set[tuple[str, str]] = set()
    now = datetime.now(timezone.utc).isoformat()

    for item in items:
        key = (item["item_type"], item["source_item_id"])
        seen_keys.add(key)
        prev = last_state.get(key)
        last_state[key] = item

        if prev is None:
            # New to us this run -> newly added to the arr-stack's wanted/library list.
            await post_event(
                client,
                {
                    "user_id": DEFAULT_USER_ID,
                    "source": "arr",
                    "source_item_id": item["source_item_id"],
                    "item_type": item["item_type"],
                    "action": "added_watchlist",
                    "timestamp": item.get("added") or now,
                    "metadata": item_metadata(item),
                },
            )

    # Anything in last_state but not in this poll's seen_keys has disappeared from arr.
    disappeared = set(last_state.keys()) - seen_keys
    for key in disappeared:
        item = last_state.pop(key)
        action = "removed" if item["has_file"] else "rejected"
        await post_event(
            client,
            {
                "user_id": DEFAULT_USER_ID,
                "source": "arr",
                "source_item_id": item["source_item_id"],
                "item_type": item["item_type"],
                "action": action,
                "timestamp": now,
                "metadata": item_metadata(item),
            },
        )


async def main() -> None:
    last_state: dict[tuple[str, str], dict] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.info("arr adapter started (radarr=%s, sonarr=%s)", bool(RADARR_URL), bool(SONARR_URL))
        while True:
            start = time.monotonic()
            try:
                await sync(client, last_state)
            except Exception:
                logger.exception("arr adapter cycle failed")
            elapsed = time.monotonic() - start
            await asyncio.sleep(max(POLL_INTERVAL_SECONDS - elapsed, 5))


if __name__ == "__main__":
    asyncio.run(main())
