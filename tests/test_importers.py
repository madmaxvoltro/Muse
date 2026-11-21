import io
import json
import zipfile

from muse_taste.importers import parse, split_netflix_title


def test_youtube_takeout_json():
    data = json.dumps([
        {"header": "YouTube", "title": "Watched Cool Video", "titleUrl": "https://www.youtube.com/watch?v=abc123",
         "subtitles": [{"name": "Chan", "url": "https://www.youtube.com/channel/UC1"}], "time": "2024-01-05T20:00:00.000Z"},
        {"header": "YouTube Music", "title": "Watched Halo", "titleUrl": "https://music.youtube.com/watch?v=m1",
         "subtitles": [{"name": "Beyoncé - Topic", "url": "x"}], "time": "2024-01-06T20:00:00.000Z"},
        {"header": "YouTube", "title": "Watched Ad", "titleUrl": "https://www.youtube.com/watch?v=ad",
         "details": [{"name": "From Google Ads"}], "time": "2024-01-06T20:00:00.000Z"},
    ]).encode()
    evs = parse("youtube", "Takeout/YouTube and YouTube Music/history/watch-history.json", data)
    assert len(evs) == 2
    assert evs[0]["item_type"] == "youtube_video" and evs[0]["metadata"]["channel"] == "Chan" and evs[0]["source_item_id"] == "abc123"
    assert evs[1]["item_type"] == "track" and evs[1]["metadata"]["artist"] == "Beyoncé"
    assert parse("youtube", "watch-history.json", data) == evs  # deterministic -> idempotent


def test_spotify_extended_and_zip():
    hist = [
        {"ts": "2024-02-01T10:00:00Z", "ms_played": 200000, "master_metadata_track_name": "Song", "master_metadata_album_artist_name": "Artist", "spotify_track_uri": "spotify:track:1", "reason_end": "trackdone", "skipped": False},
        {"ts": "2024-02-01T10:05:00Z", "ms_played": 5000, "master_metadata_track_name": "Other", "master_metadata_album_artist_name": "Artist", "spotify_track_uri": "spotify:track:2", "skipped": True},
        {"ts": "2024-02-01T10:06:00Z", "ms_played": 900000, "episode_name": "Podcast"},
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2024.json", json.dumps(hist))
        z.writestr("Spotify Account Data/YourLibrary.json", json.dumps({"tracks": [{"artist": "A", "track": "T", "uri": "spotify:track:9"}]}))
    evs = parse("spotify", "my_spotify_data.zip", buf.getvalue())
    actions = sorted(e["action"] for e in evs)
    assert actions == ["completed", "favorited", "skipped"]


def test_letterboxd_ratings_map_to_thumbs():
    csv = "Date,Name,Year,Letterboxd URI,Rating\n2023-01-01,Arrival,2016,https://boxd.it/a,5\n2023-01-02,Meh,2010,https://boxd.it/b,3\n2023-01-03,Bad,2011,https://boxd.it/c,1.5\n"
    evs = parse("letterboxd", "ratings.csv", csv.encode())
    vals = {e["metadata"]["title"]: e["metadata"]["value"] for e in evs if e["action"] == "rated"}
    assert vals == {"Arrival": 2, "Bad": -1}
    assert [e["metadata"]["title"] for e in evs if e["action"] == "completed"] == ["Meh"]  # neutral = watched


def test_imdb_ratings_carry_imdb_id():
    csv = "Const,Your Rating,Date Rated,Title,URL,Title Type,IMDb Rating,Runtime (mins),Year,Genres,Num Votes,Release Date,Directors\n" \
          "tt0133093,10,2020-05-01,The Matrix,u,Movie,8.7,136,1999,\"Action, Sci-Fi\",1,1999-03-31,Lana Wachowski\n" \
          "tt0944947,8,2020-05-02,Game of Thrones,u,TV Series,9.2,57,2011,Drama,1,2011,\n"
    evs = parse("imdb", "ratings.csv", csv.encode())
    rated = [e for e in evs if e["action"] == "rated"]
    assert {e["metadata"]["external_ids"]["imdb"] for e in rated} == {"tt0133093", "tt0944947"}
    assert {e["item_type"] for e in rated} == {"movie", "series"}


def test_netflix_profiles_trailers_and_series():
    csv = "Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,Device Type,Bookmark,Latest Bookmark,Country\n" \
          "Max,2024-01-01 20:00:00,00:48:00,,Dark: Season 1: Secrets,,TV,,,NL\n" \
          "Max,2024-01-01 21:00:00,00:01:30,,Dark: Season 2 (Trailer),TRAILER,TV,,,NL\n" \
          "Kid,2024-01-02 20:00:00,01:40:00,,Frozen,,TV,,,NL\n" \
          "Max,2024-01-03 20:00:00,01:55:00,,Mission: Impossible,,TV,,,NL\n"
    evs = parse("netflix", "ViewingActivity.csv", csv.encode(), {"profile": "Max"})
    assert len(evs) == 2
    ep = next(e for e in evs if e["item_type"] == "series_episode")
    assert ep["metadata"]["series_title"] == "Dark" and ep["action"] == "completed"
    assert split_netflix_title("Mission: Impossible") == (None, "Mission: Impossible")


def test_trakt_history_and_ratings():
    hist = [{"id": 1, "watched_at": "2024-01-01T00:00:00.000Z", "type": "movie", "movie": {"title": "Heat", "year": 1995, "ids": {"trakt": 5, "tmdb": 949, "imdb": "tt0113277"}}},
            {"rated_at": "2024-01-02T00:00:00.000Z", "rating": 9, "type": "show", "show": {"title": "The Wire", "year": 2002, "ids": {"trakt": 7, "tmdb": 1438}}}]
    evs = parse("trakt", "history.json", json.dumps(hist).encode())
    assert evs[0]["metadata"]["external_ids"]["tmdb"] == "949" and evs[0]["action"] == "completed"
    assert evs[1]["action"] == "rated" and evs[1]["metadata"]["value"] == 2 and evs[1]["item_type"] == "series"


def test_soundcloud_url_list():
    evs = parse("soundcloud", "likes.txt", b"https://soundcloud.com/some-artist/great-track\nnot a url\n")
    assert evs[0]["metadata"]["artist"] == "some artist" and evs[0]["metadata"]["title"] == "great track"
