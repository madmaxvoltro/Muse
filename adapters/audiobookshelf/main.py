"""Audiobookshelf adapter. A poller with an admin API token.

* Catalog (daily): every book in every book library -> `cataloged` (title, author, narrator,
  series, genres, description) as item_type "audiobook".
* Progress (every 5 min): each user's media progress (GET /api/users/{id}) ->
  `played` once per book per day while listening, `completed` when finished (once),
  `removed` when progress is hidden/abandoned is not inferred (no signal).
  Listening sessions give the device ("Android app", "Web") for context.

Identity: Muse user id = uuid5(namespace, "audiobookshelf:<user id>"); link via link codes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("muse.audiobookshelf_adapter")

ABS_URL = os.environ.get("AUDIOBOOKSHELF_URL", "").rstrip("/")
ABS_TOKEN = os.environ.get("AUDIOBOOKSHELF_TOKEN", "")
INGESTION_URL = os.environ.get("MUSE_INGESTION_URL", "").rstrip("/")
ADAPTER_KEY = os.environ.get("MUSE_ADAPTER_KEY", "")
PROGRESS_SECONDS = int(os.environ.get("MUSE_PROGRESS_INTERVAL_SECONDS", "300"))
CATALOG_SECONDS = int(os.environ.get("MUSE_CATALOG_SYNC_INTERVAL_SECONDS", "86400"))
NS = uuid.UUID("3b0d7c2e-1f4a-4e6b-8a9c-5d2e7f1a3c48")


def muse_user(abs_user_id: str) -> str:
    return str(uuid.uuid5(NS, f"audiobookshelf:{abs_user_id}"))


async def get(client: httpx.AsyncClient, path: str, **params) -> dict:
    r = await client.get(f"{ABS_URL}{path}", params=params, headers={"Authorization": f"Bearer {ABS_TOKEN}"})
    r.raise_for_status()
    return r.json()


def book_md(item: dict) -> dict:
    meta = ((item.get("media") or {}).get("metadata")) or {}
    authors = meta.get("authorName") or ", ".join(a.get("name", "") for a in meta.get("authors", []) or [])
    series = meta.get("seriesName") or ", ".join(s.get("name", "") for s in meta.get("series", []) or [])
    md = {"title": meta.get("title"), "author": authors, "narrator": meta.get("narratorName"), "series": series,
          "genres": meta.get("genres") or [], "tags": (item.get("media") or {}).get("tags") or [],
          "year": meta.get("publishedYear"), "description": (meta.get("description") or "")[:1500],
          "duration_s": round((item.get("media") or {}).get("duration") or 0) or None}
    return {k: v for k, v in md.items() if v not in (None, "", [])}


async def post(client: httpx.AsyncClient, event: dict) -> bool:
    try:
        r = await client.post(f"{INGESTION_URL}/events", json=event, headers={"X-Muse-Api-Key": ADAPTER_KEY})
        return r.status_code < 300 or (400 <= r.status_code < 500 and r.status_code != 429)
    except httpx.HTTPError as e:
        log.warning("posting failed: %s", e)
        return False


async def catalog(client: httpx.AsyncClient) -> dict[str, dict]:
    books: dict[str, dict] = {}
    for lib in (await get(client, "/api/libraries")).get("libraries", []):
        if lib.get("mediaType") != "book":
            continue
        page = 0
        while True:
            body = await get(client, f"/api/libraries/{lib['id']}/items", limit=200, page=page, expanded=1)
            for it in body.get("results", []):
                books[it["id"]] = book_md(it)
                await post(client, {"user_id": muse_user("_library"), "source": "audiobookshelf", "source_item_id": it["id"], "item_type": "audiobook",
                                    "action": "cataloged", "metadata": {**books[it["id"]], "dedupe_key": f"abs:cat:{it['id']}"}})
            if len(body.get("results", [])) < 200:
                break
            page += 1
    return books


def progress_events(user: dict, books: dict[str, dict], today: str) -> list[dict]:
    out = []
    for mp in user.get("mediaProgress", []) or []:
        item = mp.get("libraryItemId")
        if not item or mp.get("episodeId"):
            continue  # podcasts: not handled yet
        md = books.get(item, {})
        uid = muse_user(user["id"])
        at = datetime.fromtimestamp((mp.get("lastUpdate") or 0) / 1000, timezone.utc).isoformat() if mp.get("lastUpdate") else None
        base = {"user_id": uid, "source": "audiobookshelf", "source_item_id": item, "item_type": "audiobook"}
        if at:
            base["timestamp"] = at
        if mp.get("isFinished"):
            out.append({**base, "action": "completed", "progress_pct": 100.0, "metadata": {**md, "dedupe_key": f"abs:done:{user['id']}:{item}"}})
        elif (mp.get("progress") or 0) >= 0.02:
            day = (at or today)[:10]
            out.append({**base, "action": "played", "progress_pct": round(100 * float(mp["progress"]), 1),
                        "metadata": {**md, "dedupe_key": f"abs:play:{user['id']}:{item}:{day}"}})
    return out


async def main() -> None:
    books: dict[str, dict] = {}
    last_catalog = -1e9
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            try:
                loop = asyncio.get_running_loop().time()
                if loop - last_catalog >= CATALOG_SECONDS:
                    books = await catalog(client)
                    last_catalog = loop
                    log.info("catalog: %d books", len(books))
                today = datetime.now(timezone.utc).date().isoformat()
                for u in (await get(client, "/api/users")).get("users", []):
                    full = await get(client, f"/api/users/{u['id']}")
                    for e in progress_events(full, books, today):
                        await post(client, e)
            except Exception:
                log.exception("audiobookshelf poll failed")
            await asyncio.sleep(PROGRESS_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
