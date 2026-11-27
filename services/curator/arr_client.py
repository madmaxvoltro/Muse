"""Radarr/Sonarr client. Deliberately the ONLY thing in Muse that can cause a file to be
added or removed — the curator's scoring/budget logic never touches disk directly, it only
ever calls through here (see docs/architecture.md Discovery/acquisition pipeline).

Add flow mirrors what Radarr/Sonarr's own UI does under the hood: a lookup call (by
tmdbId/tvdbId) returns a full item template, which gets merged with a quality profile,
root folder and the curator's tag before POSTing to actually add it. Quality profile and
root folder are configurable per install (RADARR_QUALITY_PROFILE_NAME / RADARR_ROOT_FOLDER
and the Sonarr equivalents); falls back to "whatever's first" if unset.

Everything the curator adds carries the tag MUSE_CURATOR_TAG (default "muse"), so it is
visible in Radarr/Sonarr's own UI. Items a person tags with it themselves are adopted
(managed from then on). Items without the tag are never touched.

Series start with season 1 only (MUSE_CURATOR_SERIES_START=first_season); the curator
monitors the next season when the household is watching (see main.run_season_cycle).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx
from muse_schema.config import secret

logger = logging.getLogger("muse.curator.arr")

API = "/api/v3"


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
    tag: str = "muse"
    series_start: str = "first_season"  # first_season | all
    _tag_ids: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ArrConfig":
        return cls(
            radarr_url=os.environ.get("RADARR_URL", ""),
            radarr_api_key=secret("RADARR_API_KEY", "") or "",
            sonarr_url=os.environ.get("SONARR_URL", ""),
            sonarr_api_key=secret("SONARR_API_KEY", "") or "",
            radarr_quality_profile_name=os.environ.get("RADARR_QUALITY_PROFILE_NAME") or None,
            radarr_root_folder=os.environ.get("RADARR_ROOT_FOLDER") or None,
            sonarr_quality_profile_name=os.environ.get("SONARR_QUALITY_PROFILE_NAME") or None,
            sonarr_root_folder=os.environ.get("SONARR_ROOT_FOLDER") or None,
            tag=(os.environ.get("MUSE_CURATOR_TAG") or "muse").strip().lower(),
            series_start=os.environ.get("MUSE_CURATOR_SERIES_START", "first_season"),
        )

    def enabled(self, item_type: str) -> bool:
        if item_type == "movie":
            return bool(self.radarr_url and self.radarr_api_key)
        return bool(self.sonarr_url and self.sonarr_api_key)


def _target(item_type: str, config: ArrConfig) -> tuple[str, dict, str | None, str | None]:
    if item_type == "movie":
        return (
            config.radarr_url.rstrip("/"),
            {"X-Api-Key": config.radarr_api_key},
            config.radarr_quality_profile_name,
            config.radarr_root_folder,
        )
    if item_type in ("series_episode", "series"):
        return (
            config.sonarr_url.rstrip("/"),
            {"X-Api-Key": config.sonarr_api_key},
            config.sonarr_quality_profile_name,
            config.sonarr_root_folder,
        )
    raise ValueError(f"arr client not applicable for item_type={item_type}")


def _endpoint(item_type: str) -> str:
    return "movie" if item_type == "movie" else "series"


async def _get(client: httpx.AsyncClient, base_url: str, headers: dict, path: str, **params):
    resp = await client.get(f"{base_url}{API}/{path}", headers=headers, params=params or None)
    resp.raise_for_status()
    return resp.json()


async def _pick_quality_profile_id(client: httpx.AsyncClient, base_url: str, headers: dict, preferred_name: str | None) -> int:
    profiles = await _get(client, base_url, headers, "qualityprofile")
    if not profiles:
        raise RuntimeError(f"no quality profiles configured at {base_url}")
    if preferred_name:
        for p in profiles:
            if p["name"] == preferred_name:
                return p["id"]
        logger.warning("quality profile '%s' not found at %s, falling back to first available", preferred_name, base_url)
    return profiles[0]["id"]


async def root_folder(client: httpx.AsyncClient, config: ArrConfig, item_type: str) -> dict:
    """The root folder the curator adds into: {path, freeSpace, ...}."""
    base_url, headers, _, preferred_path = _target(item_type, config)
    folders = await _get(client, base_url, headers, "rootfolder")
    if not folders:
        raise RuntimeError(f"no root folders configured at {base_url}")
    if preferred_path:
        for f in folders:
            if f["path"].rstrip("/") == preferred_path.rstrip("/"):
                return f
        logger.warning("root folder '%s' not found at %s, falling back to first available", preferred_path, base_url)
    return folders[0]


async def free_bytes(client: httpx.AsyncClient, config: ArrConfig, item_type: str) -> int | None:
    """Free space on the disk the curator adds to, as Radarr/Sonarr see it."""
    try:
        f = await root_folder(client, config, item_type)
    except (httpx.HTTPError, RuntimeError):
        logger.warning("could not read free space for %s", item_type)
        return None
    free = f.get("freeSpace")
    return int(free) if free is not None else None


async def ensure_tag(client: httpx.AsyncClient, config: ArrConfig, item_type: str) -> int:
    """The id of the curator's tag in Radarr/Sonarr, created when missing."""
    which = _endpoint(item_type)
    if which in config._tag_ids:
        return config._tag_ids[which]
    base_url, headers, _, _ = _target(item_type, config)
    for t in await _get(client, base_url, headers, "tag"):
        if str(t.get("label", "")).lower() == config.tag:
            config._tag_ids[which] = int(t["id"])
            return config._tag_ids[which]
    resp = await client.post(f"{base_url}{API}/tag", headers=headers, json={"label": config.tag})
    resp.raise_for_status()
    config._tag_ids[which] = int(resp.json()["id"])
    return config._tag_ids[which]


def _first_season_only(seasons: list[dict]) -> tuple[list[dict], int]:
    """Monitor the first real season (specials, season 0, stay off)."""
    numbers = sorted(s["seasonNumber"] for s in seasons if s.get("seasonNumber", 0) > 0)
    first = numbers[0] if numbers else 1
    return [{**s, "monitored": s.get("seasonNumber") == first} for s in seasons], first


async def add_wanted(client: httpx.AsyncClient, config: ArrConfig, item_type: str, tmdb_or_tvdb_id: str) -> str:
    """Adds an item to Radarr/Sonarr's wanted list; Radarr/Sonarr decide the actual
    release/quality and perform the download — Muse only expresses intent. Returns the
    arr-internal item ID (needed later to remove it — distinct from tmdb/tvdb id).
    """
    base_url, headers, quality_name, _ = _target(item_type, config)
    quality_profile_id = await _pick_quality_profile_id(client, base_url, headers, quality_name)
    root_folder_path = (await root_folder(client, config, item_type))["path"]
    tag_id = await ensure_tag(client, config, item_type)

    if item_type == "movie":
        lookup = await client.get(f"{base_url}{API}/movie/lookup/tmdb", headers=headers, params={"tmdbId": tmdb_or_tvdb_id})
        lookup.raise_for_status()
        payload = lookup.json()
        if isinstance(payload, list):  # some Radarr versions answer with a list
            if not payload:
                raise RuntimeError(f"Radarr lookup returned nothing for tmdbId={tmdb_or_tvdb_id}")
            payload = payload[0]
        if payload.get("id"):  # already in Radarr (added by a person): never take it over silently
            raise RuntimeError(f"tmdbId={tmdb_or_tvdb_id} is already in Radarr")
        payload.update(
            qualityProfileId=quality_profile_id,
            rootFolderPath=root_folder_path,
            monitored=True,
            tags=sorted(set(payload.get("tags") or []) | {tag_id}),
            addOptions={"searchForMovie": True},
        )
        resp = await client.post(f"{base_url}{API}/movie", headers=headers, json=payload)
    else:
        lookup = await client.get(f"{base_url}{API}/series/lookup", headers=headers, params={"term": f"tvdb:{tmdb_or_tvdb_id}"})
        lookup.raise_for_status()
        results = lookup.json()
        if not results:
            raise RuntimeError(f"Sonarr lookup returned nothing for tvdbId={tmdb_or_tvdb_id}")
        payload = results[0]
        if payload.get("id"):
            raise RuntimeError(f"tvdbId={tmdb_or_tvdb_id} is already in Sonarr")
        add_options: dict = {"searchForMissingEpisodes": True}
        if config.series_start == "first_season" and payload.get("seasons"):
            payload["seasons"], _ = _first_season_only(payload["seasons"])
            add_options["monitor"] = "firstSeason"  # Sonarr v4; v3 reads the per-season flags above
        payload.update(
            qualityProfileId=quality_profile_id,
            rootFolderPath=root_folder_path,
            seriesType=payload.get("seriesType") or "standard",
            seasonFolder=True,
            monitored=True,
            tags=sorted(set(payload.get("tags") or []) | {tag_id}),
            addOptions=add_options,
        )
        resp = await client.post(f"{base_url}{API}/series", headers=headers, json=payload)

    resp.raise_for_status()
    arr_id = str(resp.json()["id"])
    logger.info("added %s tmdb/tvdb=%s to arr as id=%s", item_type, tmdb_or_tvdb_id, arr_id)
    return arr_id


async def remove_item(client: httpx.AsyncClient, config: ArrConfig, item_type: str, arr_item_id: str) -> None:
    """Removes an item and its downloaded files via the arr API. `arr_item_id` is the
    arr-internal ID returned by add_wanted, NOT the tmdb/tvdb id. An item already gone
    (removed by hand in Radarr/Sonarr) counts as removed.
    """
    base_url, headers, _, _ = _target(item_type, config)
    resp = await client.delete(
        f"{base_url}{API}/{_endpoint(item_type)}/{arr_item_id}",
        headers=headers,
        params={"deleteFiles": "true", "addImportExclusion": "false"},
    )
    if resp.status_code == 404:
        logger.info("%s arr_id=%s was already gone", item_type, arr_item_id)
        return
    resp.raise_for_status()
    logger.info("removed %s arr_id=%s (files deleted)", item_type, arr_item_id)


@dataclass
class ArrItem:
    arr_id: str
    item_type: str  # 'movie' | 'series_episode' (the curator's arr-side types)
    tmdb_id: str | None
    tvdb_id: str | None
    title: str
    year: int | None
    size_bytes: int
    tagged: bool
    seasons_monitored: int | None = None
    seasons_total: int | None = None
    episode_files: int = 0
    raw: dict | None = None


async def list_items(client: httpx.AsyncClient, config: ArrConfig, item_type: str) -> list[ArrItem]:
    """Everything in Radarr (movies) or Sonarr (series), with real sizes and the tag flag."""
    if not config.enabled(item_type):
        return []
    base_url, headers, _, _ = _target(item_type, config)
    tag_id = await ensure_tag(client, config, item_type)
    out = []
    for r in await _get(client, base_url, headers, _endpoint(item_type)):
        tagged = tag_id in (r.get("tags") or [])
        if item_type == "movie":
            size = r.get("sizeOnDisk") or (r.get("movieFile") or {}).get("size") or 0
            out.append(ArrItem(str(r["id"]), "movie", str(r["tmdbId"]) if r.get("tmdbId") else None, None,
                               r.get("title") or "", r.get("year"), int(size), tagged, raw=r))
        else:
            stats = r.get("statistics") or {}
            seasons = [s for s in r.get("seasons") or [] if s.get("seasonNumber", 0) > 0]
            monitored = [s["seasonNumber"] for s in seasons if s.get("monitored")]
            out.append(ArrItem(str(r["id"]), "series_episode", str(r["tmdbId"]) if r.get("tmdbId") else None,
                               str(r["tvdbId"]) if r.get("tvdbId") else None, r.get("title") or "", r.get("year"),
                               int(stats.get("sizeOnDisk") or 0), tagged,
                               seasons_monitored=max(monitored) if monitored else 0, seasons_total=len(seasons),
                               episode_files=int(stats.get("episodeFileCount") or 0), raw=r))
    return out


async def monitor_next_season(client: httpx.AsyncClient, config: ArrConfig, arr_id: str) -> int | None:
    """Monitor the first unmonitored real season after the highest monitored one and search
    for it. Returns the season number, or None when there is no next season."""
    base_url, headers, _, _ = _target("series_episode", config)
    series = await _get(client, base_url, headers, f"series/{arr_id}")
    seasons = series.get("seasons") or []
    monitored = [s["seasonNumber"] for s in seasons if s.get("monitored") and s.get("seasonNumber", 0) > 0]
    current = max(monitored) if monitored else 0
    nxt = min((s["seasonNumber"] for s in seasons if s.get("seasonNumber", 0) > current), default=None)
    if nxt is None:
        return None
    for s in seasons:
        if s.get("seasonNumber") == nxt:
            s["monitored"] = True
    resp = await client.put(f"{base_url}{API}/series/{arr_id}", headers=headers, json=series)
    resp.raise_for_status()
    cmd = await client.post(f"{base_url}{API}/command", headers=headers,
                            json={"name": "SeasonSearch", "seriesId": int(arr_id), "seasonNumber": nxt})
    cmd.raise_for_status()
    logger.info("series arr_id=%s: monitoring season %s", arr_id, nxt)
    return nxt


async def check(client: httpx.AsyncClient, url: str, api_key: str) -> tuple[bool, str]:
    """For setup and status: can we reach this Radarr/Sonarr, and what is it?"""
    try:
        resp = await client.get(f"{url.rstrip('/')}{API}/system/status", headers={"X-Api-Key": api_key})
    except httpx.HTTPError as exc:
        return False, f"unreachable ({exc.__class__.__name__})"
    if resp.status_code == 401:
        return False, "API key rejected"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    info = resp.json()
    return True, f"{info.get('appName') or 'arr'} {info.get('version', '')}".strip()
