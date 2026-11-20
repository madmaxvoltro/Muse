"""Security fixes from docs/security-audit.md."""

from __future__ import annotations

import io
import zipfile

import pytest

from test_flow import ADMIN, as_jf, as_mono, world  # noqa: F401


async def test_no_ssrf_through_webhooks_or_calendars(world):
    c = world["client"]
    for url in ("http://127.0.0.1:5432/", "http://169.254.169.254/latest/meta-data", "http://localhost/x"):
        r = await c.post("/v1/webhooks", json={"url": url, "events": ["digest"]}, headers=as_mono())
        assert r.status_code == 422, url
        r = await c.post("/v1/users/me/calendars", json={"url": url}, headers=as_mono())
        assert r.status_code == 422, url
    # An admin may point a household hook at the LAN (Home Assistant).
    assert (await c.post("/v1/webhooks", json={"url": "http://127.0.0.1:8123/api/webhook/muse", "events": ["*"], "user": None},
                         headers=ADMIN)).status_code == 200


async def test_link_code_guessing_is_rate_limited(world):
    import sys

    sys.modules["queryapi.guards"].link_failures.hits.clear()
    c = world["client"]
    codes = [(await c.post("/v1/identity/link", json={"code": f"AAAA-{i:04d}"}, headers=as_mono())).status_code for i in range(12)]
    assert codes[:10] == [400] * 10 and codes[-1] == 429


async def test_tokens_can_expire(world, db):
    import asyncpg

    c = world["client"]
    tok = (await c.post("/v1/tokens", json={"label": "short", "expires_days": 1}, headers=as_mono())).json()
    assert tok["expires_at"]
    conn = await asyncpg.connect(db)
    await conn.execute("UPDATE api_token SET expires_at = now() - interval '1 minute' WHERE id = $1::uuid", tok["id"])
    await conn.close()
    r = await c.get("/v1/me", headers={"Authorization": f"Bearer {tok['token']}"})
    assert r.status_code == 401 and "expired" in r.json()["detail"]


async def test_zip_bombs_are_refused(world):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(3):
            z.writestr(f"Streaming_History_Audio_{i}.json", b"[" + b" " * 400_000_000 + b"]")
    r = await world["client"].post("/v1/users/me/imports/spotify", files={"files": ("export.zip", buf.getvalue())}, headers=as_mono())
    assert r.status_code == 422 and "too large" in r.json()["detail"]
