"""Per-adapter API key auth. Each adapter gets its own key (issued via Infisical in prod,
see docs/installation.md) so a compromised adapter's blast radius is limited to its own key
being revoked, and requests are attributable to a specific adapter in logs.
"""

import hmac

from fastapi import Header, HTTPException
from muse_schema.config import key_list

# MUSE_ADAPTER_KEYS_FILE (a Docker secret, one "source:key" per line) or MUSE_ADAPTER_KEYS
# (comma separated, development). Each adapter has its own key.
_KEYS: dict[str, str] = key_list("MUSE_ADAPTER_KEYS")


async def require_adapter_key(x_muse_api_key: str = Header(...)) -> str:
    """Returns the source name the key belongs to, or raises 401. Constant-time comparison."""
    for key, source in _KEYS.items():
        if hmac.compare_digest(key.encode(), x_muse_api_key.encode()):
            return source
    raise HTTPException(status_code=401, detail="invalid or missing X-Muse-Api-Key")
