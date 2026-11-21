"""A tiny library + histories shaped exactly like the adapters/importers send them."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from muse_taste.tmdb import parse_details

NOW = datetime.now(timezone.utc)
USER_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

GENRE_WORDS = {
    "Crime": "detective murder investigation police noir gritty conspiracy",
    "Animation": "animated magical whimsical spirits family fantasy adventure",
    "Horror": "haunted demon terror possession gore slasher scream",
}

# tmdb id -> (title, genre, year)
MOVIES = {}
tid = 100
for g in GENRE_WORDS:
    for i in range(6):
        MOVIES[tid] = (f"{g} Film {i}", g, 2000 + i)
        tid += 1
SERIES_TMDB = 500
EXTERNAL_REC = 900  # recommended by TMDB, not in the library


def jf_item_md(tmdb_id: int) -> dict:
    title, genre, year = MOVIES[tmdb_id]
    return {"title": title, "description": f"A {genre.lower()} story. {GENRE_WORDS[genre]}", "genres": [genre], "year": year,
            "provider_ids": {"Tmdb": str(tmdb_id)}}


class FakeTMDB:
    def __init__(self):
        self.calls = 0

    async def find(self, external_id, source):
        return None

    async def search(self, kind, title, year=None):
        self.calls += 1
        for k, (t, _, y) in MOVIES.items():
            if kind == "movie" and t.lower() == title.lower():
                return k
        if kind == "tv" and title.lower() == "the long case":
            return SERIES_TMDB
        return None

    async def details(self, kind, tmdb_id):
        self.calls += 1
        if kind == "tv":
            d = {"id": tmdb_id, "name": "The Long Case", "first_air_date": "2019-01-01", "overview": "A detective series. " + GENRE_WORDS["Crime"],
                 "genres": [{"name": "Crime"}], "keywords": {"results": [{"name": "detective"}]}, "created_by": [{"name": "Show Runner"}]}
            return parse_details("tv", d)
        if tmdb_id == EXTERNAL_REC:
            return None
        title, genre, year = MOVIES[tmdb_id]
        recs = [{"id": EXTERNAL_REC, "title": "Shadow Detective", "overview": "Noir detective murder investigation.", "genre_ids": [80],
                 "release_date": "2021-05-01", "vote_average": 8.1, "vote_count": 3000}] if genre == "Crime" else []
        d = {"id": tmdb_id, "title": title, "release_date": f"{year}-01-01", "overview": f"{GENRE_WORDS[genre]}",
             "genres": [{"name": genre}], "keywords": {"keywords": [{"name": f"{genre.lower()} keyword"}]},
             "credits": {"crew": [{"name": f"{genre} Director", "job": "Director", "department": "Directing"}], "cast": []},
             "vote_average": 7.0, "vote_count": 1000, "recommendations": {"results": recs}}
        return parse_details("movie", d, {80: "Crime"})


async def insert(conn, user, source, sid, item_type, action, days_ago, weight=0.0, progress=None, md=None):
    await conn.execute(
        """INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, "timestamp", progress_pct, metadata)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)""",
        user, source, sid, item_type, action, weight, NOW - timedelta(days=days_ago), progress, md or {},
    )


async def seed(conn):
    # library catalog (as the Jellyfin adapter's catalog sync sends it)
    for tmdb_id in MOVIES:
        await insert(conn, USER_A, "jellyfin", f"jf{tmdb_id}", "movie", "cataloged", 100, md=jf_item_md(tmdb_id))
    await insert(conn, USER_A, "jellyfin", "jfseries", "series", "cataloged", 100,
                 md={"title": "The Long Case", "description": "detective series", "genres": ["Crime"], "provider_ids": {"Tmdb": str(SERIES_TMDB)}})
    # user A: loves crime, liked one animation, dropped horror early, binged the crime series
    for i, tmdb_id in enumerate([100, 101, 102, 103]):
        for p in (20, 50, 80):
            await insert(conn, USER_A, "jellyfin", f"jf{tmdb_id}", "movie", "played", 60 - i * 5, 0.3, p, jf_item_md(tmdb_id))
        await insert(conn, USER_A, "jellyfin", f"jf{tmdb_id}", "movie", "completed", 60 - i * 5, 1.0, 100, jf_item_md(tmdb_id))
    await insert(conn, USER_A, "jellyfin", "jf106", "movie", "rated", 30, 2.0, md={**jf_item_md(106), "value": 2})
    await insert(conn, USER_A, "jellyfin", "jf112", "movie", "played", 40, 0.3, 9, jf_item_md(112))
    for ep in range(8):
        await insert(conn, USER_A, "jellyfin", f"jfep{ep}", "series_episode", "completed", 20 - ep, 1.0, 100,
                     {"title": f"Episode {ep}", "series_id": "jfseries", "series_name": "The Long Case"})
    # user A also imported Letterboxd: five stars for a crime film by title + year only
    await insert(conn, USER_A, "letterboxd", "https://boxd.it/x", "movie", "rated", 200, 2.0,
                 md={"title": "Crime Film 4", "year": 2004, "value": 2, "dedupe_key": "lb:rated:x", "imported": True})
    # Netflix episodes of the same show, no ids at all
    await insert(conn, USER_A, "netflix", "The Long Case::Season 2: Ep 1", "series_episode", "completed", 5, 1.0,
                 md={"title": "Season 2: Ep 1", "series_title": "The Long Case"})
    # user B shares some crime taste
    for tmdb_id in (100, 101, 104, 105):
        await insert(conn, USER_B, "jellyfin", f"jf{tmdb_id}", "movie", "completed", 10, 1.0, 100, jf_item_md(tmdb_id))
