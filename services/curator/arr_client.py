"""Thin Radarr/Sonarr client. Deliberately the ONLY thing in Muse that can cause a file to be
added or removed — the curator's scoring/budget logic never touches disk directly, it only
ever calls through here (see docs/architecture.md Discovery/acquisition pipeline).

v1 implements the two calls the curator needs. Extend as needed; this is intentionally not a
full Radarr/Sonarr API client.
"""

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("muse.curator.arr")


@dataclass
class ArrConfig:
    radarr_url: str
    radarr_api_key: str
    sonarr_url: str
    sonarr_api_key: str


def _client_for(item_type: str, config: ArrConfig) -> tuple[str, dict]:
    if item_type == "movie":
        return config.radarr_url.rstrip("/"), {"X-Api-Key": config.radarr_api_key}
    if item_type == "series_episode":
        return config.sonarr_url.rstrip("/"), {"X-Api-Key": config.sonarr_api_key}
    raise ValueError(f"arr client not applicable for item_type={item_type}")


async def add_wanted(client: httpx.AsyncClient, config: ArrConfig, item_type: str, tmdb_or_tvdb_id: str) -> None:
    """Adds an item to Radarr/Sonarr's wanted list. Radarr/Sonarr themselves decide the
    actual release/quality and perform the download — Muse only expresses intent.
    """
    base_url, headers = _client_for(item_type, config)
    endpoint = "/api/v3/movie" if item_type == "movie" else "/api/v3/series"
    # NOTE: Radarr/Sonarr require a lookup call first (by tmdbId/tvdbId) to get the full
    # payload their add-endpoint expects (title, qualityProfileId, rootFolderPath, etc).
    # Left as a TODO — the exact payload depends on which quality profile / root folder
    # the user has configured, which isn't known generically. See docs/adapters/arr.md
    # (not yet written) for the real implementation once a specific Radarr/Sonarr install
    # is available to test against.
    logger.info("TODO: POST %s%s for %s id=%s (not yet wired to real Radarr/Sonarr payload)", base_url, endpoint, item_type, tmdb_or_tvdb_id)


async def remove_item(client: httpx.AsyncClient, config: ArrConfig, item_type: str, arr_item_id: str) -> None:
    """Removes an item (and, in Radarr/Sonarr terms, optionally its files) via the arr API."""
    base_url, headers = _client_for(item_type, config)
    endpoint = f"/api/v3/{'movie' if item_type == 'movie' else 'series'}/{arr_item_id}"
    logger.info("TODO: DELETE %s%s (not yet wired to real Radarr/Sonarr)", base_url, endpoint)
