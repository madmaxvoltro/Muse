"""Reading configuration and secrets the same way in every service.

Secrets come from files (Docker secrets under /run/secrets), never from environment variables
in production: env vars show up in `docker inspect`, crash dumps and child processes.
`secret("X")` reads X_FILE if set, else X (for development), else the default.
"""

from __future__ import annotations

import os
from urllib.parse import quote


def secret(name: str, default: str | None = None) -> str | None:
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            return default
    return os.environ.get(name, default)


def database_url() -> str:
    """MUSE_DATABASE_URL if given (development), else built from per-service role settings:
    MUSE_DB_USER + MUSE_DB_PASSWORD(_FILE) + MUSE_DB_HOST/PORT/NAME."""
    url = os.environ.get("MUSE_DATABASE_URL")
    if url:
        return url
    user = os.environ.get("MUSE_DB_USER", "muse")
    password = secret("MUSE_DB_PASSWORD", "")
    host = os.environ.get("MUSE_DB_HOST", "postgres")
    port = os.environ.get("MUSE_DB_PORT", "5432")
    name = os.environ.get("MUSE_DB_NAME", "muse")
    return f"postgresql://{quote(user)}:{quote(password or '')}@{host}:{port}/{name}"


def key_list(name: str) -> dict[str, str]:
    """'source:key' pairs, comma or newline separated, from NAME_FILE or NAME -> {key: source}."""
    raw = secret(name, "") or ""
    out: dict[str, str] = {}
    for pair in raw.replace("\n", ",").split(","):
        pair = pair.strip()
        if ":" in pair and not pair.startswith("#"):
            source, key = pair.split(":", 1)
            if key.strip():
                out[key.strip()] = source.strip()
    return out
