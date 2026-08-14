"""Per-adapter API key auth. Each adapter gets its own key (issued via Infisical in prod,
see docs/installation.md) so a compromised adapter's blast radius is limited to its own key
being revoked, and requests are attributable to a specific adapter in logs.
"""

import os

from fastapi import Header, HTTPException

# MUSE_ADAPTER_KEYS="jellyfin:xxxx,searxng:yyyy,..." — simple env-based key->source map for now.
_KEYS: dict[str, str] = {}
for pair in os.environ.get("MUSE_ADAPTER_KEYS", "").split(","):
    if ":" in pair:
        source, key = pair.split(":", 1)
        _KEYS[key] = source


async def require_adapter_key(x_muse_api_key: str = Header(...)) -> str:
    """Returns the source name the key belongs to, or raises 401."""
    source = _KEYS.get(x_muse_api_key)
    if source is None:
        raise HTTPException(status_code=401, detail="invalid or missing X-Muse-Api-Key")
    return source
