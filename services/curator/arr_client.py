"""Radarr/Sonarr client. Deliberately the ONLY thing in Muse that can cause a file to be
added or removed — the curator's scoring/budget logic never touches disk directly, it only
ever calls through here (see docs/architecture.md Discovery/acquisition pipeline).

Add flow mirrors what Radarr/Sonarr's own UI does under the hood: a lookup call (by
tmdbId/tvdbId) returns a full item template, which gets merged with a quality profile
and root folder before POSTing to actually add it. Both are configurable per-install via
env vars (RADARR_QUALITY_PROFILE_NAME / RADARR_ROOT_FOLDER, and the Sonarr equivalents);
falls back to "whatever's first" if unset, since a fresh install may only have one of each.
"""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("muse.curator.arr")


@dataclass
class ArrConfig:
    radarr_url: str
    radarr_api_key: str
    sonarr_url: str
    sonarr_api_key: str
    radarr_quality_profile_name: str | None = None
    radarr_root_folder: str | None = None
    sonarr_quality_profile_name: str | None = None
    sonarr_root_folder: str | None = None

    @classmethod
    def from_env(cls) -> "ArrConfig":
        return cls(
            radarr_url=os.environ.get("RADARR_URL", ""),
            radarr_api_key=os.environ.get("RADARR_API_KEY", ""),
            sonarr_url=os.environ.get("SONARR_URL", ""),
            sonarr_api_key=os.environ.get("SONARR_API_KEY", ""),
            radarr_quality_profile_name=os.environ.get("RADARR_QUALITY_PROFILE_NAME") or None,
            radarr_root_folder=os.environ.get("RADARR_ROOT_FOLDER") or None,
            sonarr_quality_profile_name=os.environ.get("SONARR_QUALITY_PROFILE_NAME") or None,
            sonarr_root_folder=os.environ.get("SONARR_ROOT_FOLDER") or None,
        )


def _target(item_type: str, config: ArrConfig) -> tuple[str, dict, str | None, str | None]:
    if item_type == "movie":
        return (
            config.radarr_url.rstrip("/"),
            {"X-Api-Key": config.radarr_api_key},
            config.radarr_quality_profile_name,
            config.radarr_root_folder,
        )
    if item_type == "series_episode":
        return (
            config.sonarr_url.rstrip("/"),
            {"X-Api-Key": config.sonarr_api_key},
            config.sonarr_quality_profile_name,
            config.sonarr_root_folder,
        )
    raise ValueError(f"arr client not applicable for item_type={item_type}")


async def _pick_quality_profile_id(client: httpx.AsyncClient, base_url: str, headers: dict, preferred_name: str | None) -> int:
    resp = await client.get(f"{base_url}/api/v3/qualityprofile", headers=headers)
    resp.raise_for_status()
    profiles = resp.json()
    if not profiles:
        raise RuntimeError(f"no quality profiles configured at {base_url}")
    if preferred_name:
        for p in profiles:
            if p["name"] == preferred_name:
                return p["id"]
        logger.warning("quality profile '%s' not found at %s, falling back to first available", preferred_name, base_url)
    return profiles[0]["id"]


async def _pick_root_folder(client: httpx.AsyncClient, base_url: str, headers: dict, preferred_path: str | None) -> str:
    resp = await client.get(f"{base_url}/api/v3/rootfolder", headers=headers)
    resp.raise_for_status()
    folders = resp.json()
    if not folders:
        raise RuntimeError(f"no root folders configured at {base_url}")
    if preferred_path:
        for f in folders:
            if f["path"] == preferred_path:
                return f["path"]
        logger.warning("root folder '%s' not found at %s, falling back to first available", preferred_path, base_url)
    return folders[0]["path"]


async def add_wanted(client: httpx.AsyncClient, config: ArrConfig, item_type: str, tmdb_or_tvdb_id: str) -> str:
    """Adds an item to Radarr/Sonarr's wanted list; Radarr/Sonarr decide the actual
    release/quality and perform the download — Muse only expresses intent. Returns the
    arr-internal item ID (needed later to remove it — distinct from tmdb/tvdb id).
    """
    base_url, headers, quality_name, root_folder = _target(item_type, config)
    quality_profile_id = await _pick_quality_profile_id(client, base_url, headers, quality_name)
    root_folder_path = await _pick_root_folder(client, base_url, headers, root_folder)

    if item_type == "movie":
        lookup = await client.get(f"{base_url}/api/v3/movie/lookup/tmdb", headers=headers, params={"tmdbId": tmdb_or_tvdb_id})
        lookup.raise_for_status()
        payload = lookup.json()
        payload.update(
            qualityProfileId=quality_profile_id,
            rootFolderPath=root_folder_path,
            monitored=True,
            addOptions={"searchForMovie": True},
        )
        resp = await client.post(f"{base_url}/api/v3/movie", headers=headers, json=payload)
    else:
        lookup = await client.get(f"{base_url}/api/v3/series/lookup", headers=headers, params={"term": f"tvdb:{tmdb_or_tvdb_id}"})
        lookup.raise_for_status()
        results = lookup.json()
        if not results:
            raise RuntimeError(f"Sonarr lookup returned nothing for tvdbId={tmdb_or_tvdb_id}")
        payload = results[0]
        payload.update(
            qualityProfileId=quality_profile_id,
            rootFolderPath=root_folder_path,
            seriesType="standard",
            monitored=True,
            addOptions={"searchForMissingEpisodes": True},
        )
        resp = await client.post(f"{base_url}/api/v3/series", headers=headers, json=payload)

    resp.raise_for_status()
    arr_id = str(resp.json()["id"])
    logger.info("added %s tmdb/tvdb=%s to arr as id=%s", item_type, tmdb_or_tvdb_id, arr_id)
    return arr_id


async def remove_item(client: httpx.AsyncClient, config: ArrConfig, item_type: str, arr_item_id: str) -> None:
    """Removes an item and its downloaded files via the arr API. `arr_item_id` is the
    arr-internal ID returned by add_wanted, NOT the tmdb/tvdb id.
    """
    base_url, headers, _, _ = _target(item_type, config)
    endpoint = "movie" if item_type == "movie" else "series"
    resp = await client.delete(
        f"{base_url}/api/v3/{endpoint}/{arr_item_id}",
        headers=headers,
        params={"deleteFiles": "true", "addImportExclusion": "false"},
    )
    resp.raise_for_status()
    logger.info("removed %s arr_id=%s (files deleted)", item_type, arr_item_id)
