"""Thin client for the recommendation API (the funnel lives there, not here)."""

from __future__ import annotations

import os
from typing import Any

import httpx

REC_URL = os.environ.get("MUSE_REC_URL", "http://recommendation-api:8001").rstrip("/")
READ_KEY = os.environ.get("MUSE_REC_READ_KEY", "")


class RecUnavailable(Exception):
    pass


async def recommendations(user_id: str, item_type: str, **params: Any) -> dict[str, Any]:
    q: dict[str, Any] = {"item_type": item_type}
    for k, v in params.items():
        if v is not None:
            q[k] = v
    headers = {"X-Muse-Read-Key": READ_KEY} if READ_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{REC_URL}/recommendations/{user_id}", params=q, headers=headers)
    except httpx.HTTPError as e:
        raise RecUnavailable(str(e)) from e
    if r.status_code >= 400:
        raise RecUnavailable(f"recommendation API answered {r.status_code}: {r.text[:200]}")
    return r.json()


async def get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    q = {k: v for k, v in params.items() if v is not None}
    headers = {"X-Muse-Read-Key": READ_KEY} if READ_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(f"{REC_URL}{path}", params=q, headers=headers)
    except httpx.HTTPError as e:
        raise RecUnavailable(str(e)) from e
    if r.status_code >= 400:
        raise RecUnavailable(f"recommendation API answered {r.status_code}: {r.text[:200]}")
    return r.json()
