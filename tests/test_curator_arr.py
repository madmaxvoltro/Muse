"""The curator against a fake Radarr/Sonarr (the real v3 API shapes), and its protection rules."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "services" / "curator"))

import arr_client  # noqa: E402
from arr_client import ArrConfig  # noqa: E402


class FakeArr:
    """Just enough of Radarr + Sonarr v3 to see what the curator sends."""

    def __init__(self):
        self.tags = [{"id": 1, "label": "kids"}]
        self.movies: dict[int, dict] = {7: {"id": 7, "tmdbId": 42, "title": "Mine", "year": 2001, "sizeOnDisk": 5 * 1024**3, "tags": []},
                                        8: {"id": 8, "tmdbId": 43, "title": "Tagged By Hand", "year": 2002, "sizeOnDisk": 3 * 1024**3, "tags": []}}
        self.series: dict[int, dict] = {}
        self.posts: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.commands: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] in ("rk", "sk")
        p, m = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if p == "/api/v3/system/status":
            return httpx.Response(200, json={"appName": "Radarr", "version": "5.9"})
        if p == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 4, "name": "Any"}, {"id": 6, "name": "HD-1080p"}])
        if p == "/api/v3/rootfolder":
            return httpx.Response(200, json=[{"id": 1, "path": "/data/movies", "freeSpace": 400 * 1024**3}])
        if p == "/api/v3/tag" and m == "GET":
            return httpx.Response(200, json=self.tags)
        if p == "/api/v3/tag" and m == "POST":
            t = {"id": len(self.tags) + 1, "label": body["label"]}
            self.tags.append(t)
            return httpx.Response(201, json=t)
        if p == "/api/v3/movie/lookup/tmdb":
            return httpx.Response(200, json={"tmdbId": int(request.url.params["tmdbId"]), "title": "Found", "year": 2020, "images": []})
        if p == "/api/v3/movie" and m == "POST":
            self.posts.append(("movie", body))
            new = {**body, "id": 100 + len(self.posts), "sizeOnDisk": 0}
            self.movies[new["id"]] = new
            return httpx.Response(201, json=new)
        if p == "/api/v3/movie" and m == "GET":
            return httpx.Response(200, json=list(self.movies.values()))
        if p.startswith("/api/v3/movie/") and m == "DELETE":
            mid = int(p.rsplit("/", 1)[1])
            if mid not in self.movies:
                return httpx.Response(404)
            self.deleted.append(p)
            assert request.url.params["deleteFiles"] == "true"
            del self.movies[mid]
            return httpx.Response(200)
        if p == "/api/v3/series/lookup":
            return httpx.Response(200, json=[{"tvdbId": 77, "tmdbId": 500, "title": "The Long Case", "year": 2019,
                                              "seasons": [{"seasonNumber": n, "monitored": True} for n in (0, 1, 2, 3)]}])
        if p == "/api/v3/series" and m == "POST":
            self.posts.append(("series", body))
            new = {**body, "id": 200 + len(self.posts), "statistics": {"sizeOnDisk": 9 * 1024**3, "episodeFileCount": 8}}
            self.series[new["id"]] = new
            return httpx.Response(201, json=new)
        if p == "/api/v3/series" and m == "GET":
            return httpx.Response(200, json=list(self.series.values()))
        if p.startswith("/api/v3/series/") and m == "GET":
            return httpx.Response(200, json=self.series[int(p.rsplit("/", 1)[1])])
        if p.startswith("/api/v3/series/") and m == "PUT":
            self.series[int(p.rsplit("/", 1)[1])] = body
            return httpx.Response(202, json=body)
        if p == "/api/v3/command":
            self.commands.append(body)
            return httpx.Response(201, json={"id": 1, **body})
        return httpx.Response(404, json={"path": p})


def config() -> ArrConfig:
    return ArrConfig(radarr_url="http://radarr:7878", radarr_api_key="rk", sonarr_url="http://sonarr:8989", sonarr_api_key="sk",
                     radarr_quality_profile_name="HD-1080p")


def test_radarr_add_tags_and_picks_profile():
    fake = FakeArr()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            cfg = config()
            arr_id = await arr_client.add_wanted(c, cfg, "movie", "603")
            return arr_id, await arr_client.free_bytes(c, cfg, "movie")

    arr_id, free = asyncio.run(go())
    kind, body = fake.posts[0]
    assert kind == "movie" and arr_id == "101"
    assert body["qualityProfileId"] == 6 and body["rootFolderPath"] == "/data/movies"
    assert body["addOptions"] == {"searchForMovie": True}
    muse_tag = next(t["id"] for t in fake.tags if t["label"] == "muse")
    assert body["tags"] == [muse_tag]
    assert free == 400 * 1024**3


def test_sonarr_add_starts_with_season_one_then_monitors_the_next():
    fake = FakeArr()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            cfg = config()
            arr_id = await arr_client.add_wanted(c, cfg, "series_episode", "77")
            items = await arr_client.list_items(c, cfg, "series_episode")
            nxt = await arr_client.monitor_next_season(c, cfg, arr_id)
            return arr_id, items, nxt

    arr_id, items, nxt = asyncio.run(go())
    _, body = fake.posts[0]
    assert {s["seasonNumber"]: s["monitored"] for s in body["seasons"]} == {0: False, 1: True, 2: False, 3: False}
    assert body["addOptions"]["monitor"] == "firstSeason"
    assert items[0].seasons_monitored == 1 and items[0].seasons_total == 3 and items[0].episode_files == 8 and items[0].tagged
    assert nxt == 2
    assert {s["seasonNumber"]: s["monitored"] for s in fake.series[int(arr_id)]["seasons"]} == {0: False, 1: True, 2: True, 3: False}
    assert fake.commands == [{"name": "SeasonSearch", "seriesId": int(arr_id), "seasonNumber": 2}]


def test_list_items_reads_real_sizes_and_the_tag():
    fake = FakeArr()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            cfg = config()
            tag = await arr_client.ensure_tag(c, cfg, "movie")
            fake.movies[8]["tags"] = [tag]
            return await arr_client.list_items(c, cfg, "movie")

    items = {i.arr_id: i for i in asyncio.run(go())}
    assert items["7"].size_bytes == 5 * 1024**3 and not items["7"].tagged
    assert items["8"].tagged and items["8"].tmdb_id == "43"


def test_remove_of_something_already_gone_is_fine():
    fake = FakeArr()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            await arr_client.remove_item(c, config(), "movie", "7")
            await arr_client.remove_item(c, config(), "movie", "7")  # 404 the second time

    asyncio.run(go())
    assert fake.deleted == ["/api/v3/movie/7"]


def test_check_reports_bad_keys():
    async def go():
        bad = httpx.MockTransport(lambda r: httpx.Response(401))
        async with httpx.AsyncClient(transport=bad) as c:
            return await arr_client.check(c, "http://radarr:7878", "nope")

    assert asyncio.run(go()) == (False, "API key rejected")


# --- protection rules -----------------------------------------------------------------------------

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)


def ev(user, action, days_ago, source="jellyfin", progress=None, weight=0.0):
    return {"user_id": user, "source": source, "action": action, "action_weight": weight, "progress_pct": progress,
            "timestamp": NOW - timedelta(days=days_ago)}


def test_protection_rules():
    from budget import protection_reason as why

    assert why([], NOW) is None
    assert why([ev("a", "completed", 10)], NOW) == "watched_recently"
    assert why([ev("a", "completed", 200)], NOW) is None  # watched long ago, nobody since: may go
    assert why([ev("a", "played", 100, progress=40)], NOW) == "in_progress"
    assert why([ev("a", "played", 100, progress=40), ev("a", "completed", 90)], NOW) is None
    assert why([ev("a", "played", 400, progress=40)], NOW) is None  # abandoned long ago
    # on someone's list
    assert why([ev("b", "added_watchlist", 120, source="vesper")], NOW) == "on_a_list"
    assert why([ev("b", "added_watchlist", 120, source="trakt"), ev("b", "removed", 60, source="vesper")], NOW) is None
    assert why([ev("b", "added_watchlist", 120), ev("b", "completed", 100)], NOW) is None
    # Radarr's own wanted list is where the curator puts things: it is not a person's list
    assert why([ev("h", "added_watchlist", 5, source="arr")], NOW) is None
    assert why([ev("h", "added_watchlist", 5, source="arr"), ev("b", "added_watchlist", 50, source="letterboxd")], NOW) == "on_a_list"
    # asked for, not yet watched
    assert why([ev("c", "requested", 40)], NOW) == "requested"
    assert why([ev("c", "requested", 200)], NOW) is None
    # favourites and love
    assert why([ev("d", "favorited", 300)], NOW) == "favourite"
    assert why([ev("d", "favorited", 300), ev("d", "unfavorited", 200)], NOW) is None
    assert why([ev("d", "rated", 300, weight=2.0)], NOW) == "loved"
    assert why([ev("d", "rated", 300, weight=2.0), ev("d", "rated", 100, weight=-1.0)], NOW) is None
