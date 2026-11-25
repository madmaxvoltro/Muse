"""The query API reads through the recommendation API and writes answers through the
ingestion API (the single write path for taste data). Tests swap in ASGI transports."""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException
from muse_schema.config import secret

RECO_URL = os.environ.get("MUSE_RECO_URL", "http://recommendation-api:8001").rstrip("/")
INGEST_URL = os.environ.get("MUSE_INGEST_URL", "http://ingestion-api:8000").rstrip("/")

_transports: dict[str, httpx.AsyncBaseTransport | None] = {"reco": None, "ingest": None}


def use_transports(reco: httpx.AsyncBaseTransport | None = None, ingest: httpx.AsyncBaseTransport | None = None) -> None:
    _transports["reco"], _transports["ingest"] = reco, ingest


def _client(which: str) -> httpx.AsyncClient:
    base = RECO_URL if which == "reco" else INGEST_URL
    return httpx.AsyncClient(base_url=base, timeout=20.0, transport=_transports[which])


async def reco(path: str, params: dict | None = None, *, missing_ok: bool = False) -> dict | None:
    key = secret("MUSE_READ_KEY", "")
    async with _client("reco") as c:
        r = await c.get(path, params={k: v for k, v in (params or {}).items() if v is not None},
                        headers={"X-Muse-Read-Key": key} if key else {})
    if r.status_code == 404 and missing_ok:
        return None
    if r.status_code >= 400:
        raise HTTPException(502 if r.status_code >= 500 else r.status_code, f"recommendation API: {r.text[:300]}")
    return r.json()


async def answer(body: dict) -> dict:
    key = secret("MUSE_ANSWER_KEY", "")
    if not key:
        raise HTTPException(503, "answers need MUSE_ANSWER_KEY (the `muse` ingestion key) on the query API")
    async with _client("ingest") as c:
        r = await c.post("/answers", json=body, headers={"X-Muse-Api-Key": key})
    if r.status_code >= 400:
        raise HTTPException(r.status_code if r.status_code < 500 else 502, f"ingestion API: {r.text[:300]}")
    return r.json()
