"""SearxNG adapter — a thin reverse-proxy sidecar sitting in front of SearxNG.

Why a sidecar and not a patch to SearxNG itself: SearxNG intentionally logs
nothing (privacy-by-design), so there's no API to poll. Patching searx/webapp.py
directly works too but has to be re-applied on every SearxNG update. A sidecar
that sits in front of it survives updates untouched (see docs/adapters/searxng.md).

Flow: nginx -> this sidecar -> SearxNG. Every /search request is logged as an
event (fire-and-forget, never blocks the actual search) then transparently
proxied through.
"""

import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response

SEARXNG_UPSTREAM = os.environ["SEARXNG_UPSTREAM_URL"]  # e.g. http://searxng:8080
INGESTION_URL = os.environ["MUSE_INGESTION_URL"]  # e.g. http://ingestion-api:8000
ADAPTER_KEY = os.environ["MUSE_ADAPTER_KEY"]
DEFAULT_USER_ID = os.environ["MUSE_DEFAULT_USER_ID"]  # fallback until identity resolution is wired up

app = FastAPI(title="Muse SearxNG Adapter")
client = httpx.AsyncClient(timeout=10.0)


async def _log_search(query: str, category: str | None) -> None:
    event = {
        "user_id": DEFAULT_USER_ID,
        "source": "searxng",
        "item_type": "search_query",
        "action": "searched",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"query": query, "category": category or "general"},
    }
    try:
        await client.post(
            f"{INGESTION_URL}/events",
            json=event,
            headers={"X-Muse-Api-Key": ADAPTER_KEY},
        )
    except httpx.HTTPError:
        # Never let ingestion failures break the actual search.
        pass


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request) -> Response:
    upstream_url = f"{SEARXNG_UPSTREAM}/{path}"
    body = await request.body()

    if path == "search":
        params = dict(request.query_params)
        query = params.get("q") or (await request.form()).get("q", "")
        if query:
            await _log_search(str(query), params.get("category_general"))

    upstream_resp = await client.request(
        request.method,
        upstream_url,
        params=request.query_params,
        content=body,
        headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
    )
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=dict(upstream_resp.headers),
    )
