"""Read and control keys for the recommendation API.

MUSE_READ_KEYS="vesper:xxxx,jellyfin-plugin:yyyy" — may call every GET endpoint.
MUSE_CONTROL_KEYS="vesper:zzzz" — may also change state on a user's behalf: Soon Gone
whitelisting, the user-control overlay (explore dial, mood), per-user wipes.

When MUSE_CONTROL_KEYS is unset, read keys may do both (backwards compatible with the
Jellyfin plugin's whitelist hook). When neither is set the API is open (dev only).
Ingestion keys (MUSE_ADAPTER_KEYS) are a separate set, owned by the ingestion API.
"""

import os

from fastapi import Header, HTTPException, Request


def _parse(env: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in os.environ.get(env, "").split(","):
        if ":" in pair:
            name, key = pair.split(":", 1)
            if key.strip():
                out[key.strip()] = name.strip()
    return out


READ_KEYS = _parse("MUSE_READ_KEYS")
CONTROL_KEYS = _parse("MUSE_CONTROL_KEYS")

OPEN_PATHS = {"/healthz"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# POSTs that only read (a body is just easier than a query string for these).
READ_ONLY_POSTS = {"/search/semantic"}


def allowed(method: str, path: str, key: str | None, read_keys: dict[str, str], control_keys: dict[str, str]) -> bool:
    if path in OPEN_PATHS:
        return True
    if not read_keys and not control_keys:
        return True  # unconfigured: open, for development
    if key is None:
        return False
    if key in control_keys:
        return True
    if method in MUTATING and path not in READ_ONLY_POSTS:
        return not control_keys and key in read_keys
    return key in read_keys


async def require_key(request: Request, x_muse_read_key: str | None = Header(default=None)) -> None:
    if not allowed(request.method, request.url.path, x_muse_read_key, READ_KEYS, CONTROL_KEYS):
        raise HTTPException(status_code=401, detail="invalid or missing X-Muse-Read-Key for this endpoint")
