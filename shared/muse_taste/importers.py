"""History importers: bootstrap a taste profile from the data exports people already have.

A new profile with years of history elsewhere shouldn't start cold. Each parser turns an
export file (or the whole export .zip) into Muse events, with `metadata.dedupe_key` so
importing the same file twice changes nothing.

  youtube     Google Takeout > YouTube and YouTube Music: watch-history.json (pick JSON in
              Takeout; the HTML variant is parsed best-effort), search-history.json,
              subscriptions.csv, liked-videos playlist CSV. YouTube Music plays become tracks.
  spotify     Privacy > "Extended streaming history" (Streaming_History_Audio_*.json) or the
              basic account data (StreamingHistory*_music_*.json, YourLibrary.json).
  soundcloud  Best effort: any CSV/JSON with title + artist/user columns, or a plain list of
              soundcloud.com track URLs (e.g. copied likes). SoundCloud's export format is not
              documented, so this parser is deliberately lenient.
  letterboxd  Settings > Data > Export: ratings.csv, diary.csv, watched.csv, watchlist.csv, likes.
  imdb        Your Ratings / Watchlist > Export (ratings.csv, watchlist.csv).
  netflix     Account > Download your personal information: ViewingActivity.csv
              (option: profile name, since a Netflix account is often shared).
  trakt       JSON exports of history / ratings / watchlist (API response shapes).

Mapping ratings to thumbs: 5-star scale (Letterboxd) >=4.5 love, >=3.5 up, <=2 down, in
between "watched" only. 10-point scale (IMDb, Trakt) >=9 love, >=7 up, <=4 down.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

KINDS = ("youtube", "spotify", "soundcloud", "letterboxd", "imdb", "netflix", "trakt")
RATED_WEIGHTS = {-1: -1.0, 1: 1.0, 2: 2.0}


class ImportError_(ValueError):
    pass


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ev(source: str, item_type: str, action: str, ts: datetime | None, sid: str, md: dict, dedupe: str, weight: float | None = None, progress: float | None = None) -> dict:
    ev: dict[str, Any] = {
        "source": source,
        "source_item_id": sid,
        "item_type": item_type,
        "action": action,
        "timestamp": _iso(ts or datetime.now(timezone.utc)),
        "metadata": {**{k: v for k, v in md.items() if v not in (None, "", [])}, "dedupe_key": dedupe, "imported": True},
    }
    if weight is not None:
        ev["action_weight"] = weight
    if progress is not None:
        ev["progress_pct"] = max(0.0, min(100.0, progress))
    return ev


def _rated(source: str, item_type: str, ts: datetime | None, sid: str, md: dict, thumbs: int, dedupe: str) -> dict:
    return _ev(source, item_type, "rated", ts, sid, {**md, "value": thumbs}, dedupe, weight=RATED_WEIGHTS[thumbs])


def _thumbs_5(r: float) -> int | None:
    if r >= 4.5:
        return 2
    if r >= 3.5:
        return 1
    if r <= 2.0:
        return -1
    return None


def _thumbs_10(r: float) -> int | None:
    if r >= 9:
        return 2
    if r >= 7:
        return 1
    if r <= 4:
        return -1
    return None


def _csv_rows(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]


def _files(filename: str, data: bytes) -> list[tuple[str, bytes]]:
    """A single file, or every member of a .zip export."""
    if data[:2] == b"PK":
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if not info.is_dir() and info.file_size < 512 * 1024 * 1024:
                    out.append((info.filename, z.read(info)))
        return out
    return [(filename, data)]


def parse(kind: str, filename: str, data: bytes, options: dict | None = None) -> list[dict]:
    options = options or {}
    if kind not in KINDS:
        raise ImportError_(f"unknown import kind {kind!r}; one of {KINDS}")
    fn = {
        "youtube": _youtube,
        "spotify": _spotify,
        "soundcloud": _soundcloud,
        "letterboxd": _letterboxd,
        "imdb": _imdb,
        "netflix": _netflix,
        "trakt": _trakt,
    }[kind]
    events: list[dict] = []
    for name, blob in _files(filename, data):
        events.extend(fn(name, blob, options))
    seen: set[str] = set()
    unique = []
    for e in events:
        k = e["metadata"]["dedupe_key"]
        if k not in seen:
            seen.add(k)
            unique.append(e)
    return unique


# ---- YouTube --------------------------------------------------------------------------------

def _yt_video_id(url: str | None) -> str | None:
    if not url:
        return None
    u = urlparse(html.unescape(url))
    if u.hostname and "youtu.be" in u.hostname:
        return u.path.strip("/") or None
    return (parse_qs(u.query).get("v") or [None])[0]


def _yt_strip(title: str, prefixes: tuple[str, ...]) -> str:
    for p in prefixes:
        if title.startswith(p):
            return title[len(p):].strip()
    return title.strip()


_WATCHED = ("Watched ", "Bekeken: ", "Bekeken ", "Angesehen: ", "A regardé ", "Visto ")
_SEARCHED = ("Searched for ", "Gezocht naar ", "Gesucht nach: ", "A recherché ")


def _youtube(name: str, data: bytes, opts: dict) -> list[dict]:
    lname = name.lower()
    out: list[dict] = []
    if lname.endswith(".json") and ("watch-history" in lname or "kijkgeschiedenis" in lname or "wiedergabeverlauf" in lname):
        for e in json.loads(data.decode("utf-8")):
            if any((d.get("name") or "").startswith("From Google Ads") for d in e.get("details", []) or []):
                continue
            vid = _yt_video_id(e.get("titleUrl"))
            if not vid:
                continue
            ts = _parse_dt(e.get("time"))
            title = _yt_strip(e.get("title", ""), _WATCHED)
            sub = (e.get("subtitles") or [{}])[0]
            channel = sub.get("name")
            channel_id = (sub.get("url") or "").rstrip("/").split("/")[-1] or None
            if e.get("header") == "YouTube Music":
                artist = re.sub(r"\s+-\s+Topic$", "", channel or "")
                out.append(_ev("youtube", "track", "played", ts, vid, {"title": title, "artist": artist, "video_id": vid}, f"ytm:{vid}:{e.get('time')}"))
            else:
                out.append(_ev("youtube", "youtube_video", "played", ts, vid, {"title": title, "channel": channel, "channel_id": channel_id, "video_id": vid}, f"yt:{vid}:{e.get('time')}"))
    elif lname.endswith(".html") and "watch-history" in lname:
        out.extend(_youtube_html(data.decode("utf-8", errors="replace")))
    elif lname.endswith(".json") and "search-history" in lname:
        for e in json.loads(data.decode("utf-8")):
            q = _yt_strip(e.get("title", ""), _SEARCHED)
            if not q or q == e.get("title"):
                url = e.get("titleUrl") or ""
                q = (parse_qs(urlparse(url).query).get("search_query") or [""])[0]
            if q:
                out.append(_ev("youtube", "search_query", "searched", _parse_dt(e.get("time")), f"q:{q}", {"query": q}, f"ytq:{q}:{e.get('time')}"))
    elif lname.endswith(".csv") and "subscription" in lname:
        for r in _csv_rows(data):
            cid = r.get("Channel Id") or r.get("Kanaal-ID") or r.get("Channel ID")
            title = r.get("Channel Title") or r.get("Kanaaltitel") or r.get("Channel title")
            if cid:
                out.append(_ev("youtube", "youtube_video", "favorited", None, f"channel:{cid}", {"title": title, "channel": title, "channel_id": cid, "kind": "channel"}, f"ytsub:{cid}"))
    elif lname.endswith(".csv") and ("liked" in lname or "leuk" in lname or "like" in lname):
        for r in _csv_rows(data):
            vid = r.get("Video ID") or r.get("Video-ID") or r.get("Video Id")
            ts = _parse_dt(r.get("Playlist Video Creation Timestamp") or r.get("Time Added") or r.get("Video toegevoegd"))
            if vid:
                out.append(_ev("youtube", "youtube_video", "favorited", ts, vid, {"video_id": vid}, f"ytlike:{vid}"))
    return out


_HTML_ENTRY = re.compile(
    r'<a href="(https://www\.youtube\.com/watch\?v=[^"]+)">(.*?)</a><br>\s*(?:<a href="https://www\.youtube\.com/channel/([^"]+)">(.*?)</a><br>)?\s*([^<]+?)<br>',
    re.S,
)


def _youtube_html(text: str) -> list[dict]:
    out = []
    for url, title, cid, channel, when in _HTML_ENTRY.findall(text):
        vid = _yt_video_id(url)
        if not vid:
            continue
        ts = None
        clean = re.sub(r"\s+[A-Z]{2,5}$", "", html.unescape(when).replace(" ", " ").strip())
        for fmt in ("%b %d, %Y, %I:%M:%S %p", "%d %b %Y, %H:%M:%S", "%d %b %Y %H:%M:%S"):
            try:
                ts = datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                pass
        out.append(_ev("youtube", "youtube_video", "played", ts, vid, {"title": html.unescape(title), "channel": html.unescape(channel) or None, "channel_id": cid or None, "video_id": vid}, f"yt:{vid}:{when.strip()}"))
    return out


# ---- Spotify --------------------------------------------------------------------------------

def _spotify(name: str, data: bytes, opts: dict) -> list[dict]:
    lname = name.lower().rsplit("/", 1)[-1]
    if not lname.endswith(".json"):
        return []
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    if lname.startswith("yourlibrary") and isinstance(payload, dict):
        for t in payload.get("tracks", []):
            artist, title = t.get("artist"), t.get("track")
            if artist and title:
                sid = t.get("uri") or f"{artist}|{title}"
                out.append(_ev("spotify", "track", "favorited", None, sid, {"title": title, "artist": artist, "album": t.get("album")}, f"splike:{sid}"))
        return out
    if not isinstance(payload, list):
        return []
    for s in payload:
        if not isinstance(s, dict):
            continue
        if "master_metadata_track_name" in s or "ts" in s:  # extended history
            title, artist = s.get("master_metadata_track_name"), s.get("master_metadata_album_artist_name")
            if not title or not artist:
                continue  # podcast episode / audiobook chapter
            ms = int(s.get("ms_played") or 0)
            ts = _parse_dt(s.get("ts"))
            sid = s.get("spotify_track_uri") or f"{artist}|{title}"
            md = {"title": title, "artist": artist, "album": s.get("master_metadata_album_album_name")}
            skipped = bool(s.get("skipped")) or ms < 30000
            action = "skipped" if skipped else ("completed" if s.get("reason_end") == "trackdone" else "played")
            out.append(_ev("spotify", "track", action, ts, sid, md, f"sp:{sid}:{s.get('ts')}"))
        elif "trackName" in s:  # basic account data
            title, artist = s.get("trackName"), s.get("artistName")
            if not title or not artist or title == "Unknown Track":
                continue
            ms = int(s.get("msPlayed") or 0)
            ts = _parse_dt(s.get("endTime"))
            sid = f"{artist}|{title}"
            action = "skipped" if ms < 30000 else "played"
            out.append(_ev("spotify", "track", action, ts, sid, {"title": title, "artist": artist}, f"sp:{sid}:{s.get('endTime')}"))
    return out


# ---- SoundCloud (lenient) --------------------------------------------------------------------

def _sc_from_url(url: str) -> tuple[str, str] | None:
    u = urlparse(url.strip())
    if not u.hostname or "soundcloud.com" not in u.hostname:
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2 or parts[1] in ("sets", "likes", "tracks", "reposts"):
        return None
    pretty = lambda s: unquote(s).replace("-", " ").strip()
    return pretty(parts[0]), pretty(parts[1])


def _soundcloud(name: str, data: bytes, opts: dict) -> list[dict]:
    lname = name.lower()
    out: list[dict] = []
    action = "favorited" if ("like" in lname or opts.get("as_likes", True)) else "played"

    def emit(title: str | None, artist: str | None, url: str | None, when: str | None) -> None:
        if (not title or not artist) and url:
            parsed = _sc_from_url(url)
            if parsed:
                artist, title = artist or parsed[0], title or parsed[1]
        if title and artist:
            sid = url or f"{artist}|{title}"
            out.append(_ev("soundcloud", "track", action, _parse_dt(when), sid, {"title": title, "artist": artist, "url": url}, f"sc:{sid}"))

    if lname.endswith(".csv"):
        for r in _csv_rows(data):
            low = {k.lower(): v for k, v in r.items()}
            title = low.get("title") or low.get("track") or low.get("track title") or low.get("name")
            artist = low.get("artist") or low.get("user") or low.get("username") or low.get("creator") or low.get("uploader")
            url = low.get("url") or low.get("permalink_url") or low.get("link") or low.get("track url")
            emit(title, artist, url, low.get("created_at") or low.get("date") or low.get("liked_at"))
    elif lname.endswith(".json"):
        payload = json.loads(data.decode("utf-8"))
        items = payload if isinstance(payload, list) else payload.get("collection") or payload.get("likes") or payload.get("tracks") or []
        for it in items:
            t = it.get("track") or it
            user = t.get("user") or {}
            emit(t.get("title"), user.get("username") if isinstance(user, dict) else t.get("artist"), t.get("permalink_url") or t.get("url"), it.get("created_at"))
    else:
        for line in data.decode("utf-8", errors="replace").splitlines():
            if "soundcloud.com" in line:
                emit(None, None, line.strip(), None)
    return out


# ---- Letterboxd -----------------------------------------------------------------------------

def _letterboxd(name: str, data: bytes, opts: dict) -> list[dict]:
    lname = name.lower().rsplit("/", 1)[-1]
    if not lname.endswith(".csv"):
        return []
    rows = _csv_rows(data)
    out: list[dict] = []
    is_likes = "likes" in name.lower() and lname == "films.csv"
    for r in rows:
        title, year, uri = r.get("Name"), r.get("Year"), r.get("Letterboxd URI")
        if not title:
            continue
        sid = uri or f"{title} ({year})"
        md = {"title": title, "year": int(year) if (year or "").isdigit() else None, "letterboxd_uri": uri}
        date = _parse_dt(r.get("Watched Date") or r.get("Date"))
        if lname == "ratings.csv" or (lname == "diary.csv" and r.get("Rating")):
            try:
                thumbs = _thumbs_5(float(r["Rating"]))
            except (KeyError, ValueError):
                thumbs = None
            if thumbs is not None:
                out.append(_rated("letterboxd", "movie", date, sid, md, thumbs, f"lb:rated:{sid}"))
            elif lname == "ratings.csv":
                # 2.5-3.5 stars: watched, no strong opinion -> counts as watched, nothing more
                out.append(_ev("letterboxd", "movie", "completed", date, sid, md, f"lb:rated-watched:{sid}"))
        if lname in ("watched.csv", "diary.csv"):
            out.append(_ev("letterboxd", "movie", "completed", date, sid, md, f"lb:watched:{sid}:{r.get('Watched Date') or r.get('Date')}"))
        elif lname == "watchlist.csv":
            out.append(_ev("letterboxd", "movie", "added_watchlist", date, sid, md, f"lb:watchlist:{sid}"))
        elif is_likes:
            out.append(_ev("letterboxd", "movie", "favorited", date, sid, md, f"lb:like:{sid}"))
    return out


# ---- IMDb -----------------------------------------------------------------------------------

_IMDB_TYPE = {
    "movie": "movie", "tvmovie": "movie", "video": "movie", "short": "movie", "tv movie": "movie", "tv short": "movie",
    "tvseries": "series", "tv series": "series", "tvminiseries": "series", "tv mini series": "series", "tv mini-series": "series",
    "tvepisode": "series_episode", "tv episode": "series_episode",
}


def _imdb(name: str, data: bytes, opts: dict) -> list[dict]:
    if not name.lower().endswith(".csv"):
        return []
    out: list[dict] = []
    for r in _csv_rows(data):
        const = r.get("Const") or r.get("const")
        if not const:
            continue
        item_type = _IMDB_TYPE.get((r.get("Title Type") or "movie").lower(), "movie")
        md = {
            "title": r.get("Title"),
            "year": int(r["Year"]) if (r.get("Year") or "").isdigit() else None,
            "genres": [g.strip() for g in (r.get("Genres") or "").split(",") if g.strip()],
            "external_ids": {"imdb": const},
        }
        if r.get("Directors"):
            md["people"] = [{"name": d.strip(), "type": "Director"} for d in r["Directors"].split(",") if d.strip()]
        if r.get("Your Rating"):
            try:
                thumbs = _thumbs_10(float(r["Your Rating"]))
            except ValueError:
                thumbs = None
            ts = _parse_dt(r.get("Date Rated"))
            out.append(_ev("imdb", item_type, "completed", ts, const, md, f"imdb:watched:{const}"))
            if thumbs is not None:
                out.append(_rated("imdb", item_type, ts, const, md, thumbs, f"imdb:rated:{const}"))
        else:
            out.append(_ev("imdb", item_type, "added_watchlist", _parse_dt(r.get("Created")), const, md, f"imdb:watchlist:{const}"))
    return out


# ---- Netflix --------------------------------------------------------------------------------

_SERIES_MARKER = re.compile(r"^(season|series|part|volume|vol\.|limited series|miniseries|book|chapter|collection|staffel|seizoen|deel)\b", re.I)


def split_netflix_title(title: str) -> tuple[str | None, str]:
    """'Dark: Season 1: Secrets' -> ('Dark', 'Season 1: Secrets'); 'Mission: Impossible' -> (None, title)."""
    parts = title.split(": ")
    if len(parts) >= 3 and _SERIES_MARKER.match(parts[1]):
        return parts[0], ": ".join(parts[1:])
    if len(parts) >= 3:
        return parts[0], ": ".join(parts[1:])
    return None, title


def _duration_s(d: str) -> int:
    try:
        h, m, s = (int(x) for x in d.split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return 0


def _netflix(name: str, data: bytes, opts: dict) -> list[dict]:
    if "viewingactivity" not in name.lower().replace("_", "").replace(" ", ""):
        return []
    want_profile = (opts.get("profile") or "").strip().lower()
    out: list[dict] = []
    for r in _csv_rows(data):
        if want_profile and (r.get("Profile Name") or "").strip().lower() != want_profile:
            continue
        if (r.get("Supplemental Video Type") or "").strip():
            continue  # trailers, hooks, recaps
        title = r.get("Title") or ""
        secs = _duration_s(r.get("Duration") or "0:0:0")
        if not title or secs < 120:
            continue
        ts = _parse_dt(r.get("Start Time"))
        series, rest = split_netflix_title(title)
        if series:
            action = "completed" if secs >= 15 * 60 else "played"
            out.append(_ev("netflix", "series_episode", action, ts, f"{series}::{rest}", {"title": rest, "series_title": series}, f"nf:{title}:{r.get('Start Time')}",
                           progress=None if action == "completed" else min(secs / 2400 * 100, 95)))
        else:
            # Runtime unknown: estimate progress against a typical 110-minute film.
            progress = min(secs / (110 * 60) * 100, 100.0)
            action = "completed" if secs >= 60 * 60 else "played"
            out.append(_ev("netflix", "movie", action, ts, title, {"title": title}, f"nf:{title}:{r.get('Start Time')}", progress=progress))
    return out


# ---- Trakt ----------------------------------------------------------------------------------

def _trakt_obj(it: dict) -> tuple[str, dict, dict] | None:
    t = it.get("type")
    if t == "movie" and it.get("movie"):
        return "movie", it["movie"], {}
    if t in ("show", "season") and it.get("show"):
        return "series", it["show"], {}
    if t == "episode" and it.get("show"):
        return "series_episode", it.get("episode") or {}, it["show"]
    return None


def _trakt(name: str, data: bytes, opts: dict) -> list[dict]:
    if not name.lower().endswith(".json"):
        return []
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for it in payload:
        parsed = _trakt_obj(it)
        if not parsed:
            continue
        item_type, obj, show = parsed
        ids = {k: str(v) for k, v in (obj.get("ids") or {}).items() if v and k in ("tmdb", "imdb", "tvdb")}
        md: dict[str, Any] = {"title": obj.get("title"), "year": obj.get("year"), "external_ids": ids}
        if show:
            md["series_title"] = show.get("title")
            md["series_external_ids"] = {k: str(v) for k, v in (show.get("ids") or {}).items() if v and k in ("tmdb", "imdb", "tvdb")}
            md["season"], md["episode"] = obj.get("season"), obj.get("number")
        sid = str((obj.get("ids") or {}).get("trakt") or ids.get("tmdb") or obj.get("title"))
        if "rating" in it:
            thumbs = _thumbs_10(float(it["rating"]))
            if thumbs is not None:
                out.append(_rated("trakt", item_type, _parse_dt(it.get("rated_at")), sid, md, thumbs, f"trakt:rated:{item_type}:{sid}"))
        elif "listed_at" in it:
            out.append(_ev("trakt", item_type, "added_watchlist", _parse_dt(it.get("listed_at")), sid, md, f"trakt:wl:{item_type}:{sid}"))
        elif "watched_at" in it:
            out.append(_ev("trakt", item_type, "completed", _parse_dt(it.get("watched_at")), sid, md, f"trakt:h:{it.get('id') or sid}:{it.get('watched_at')}"))
    return out


def with_user(events: Iterable[dict], user_id: str) -> list[dict]:
    return [{**e, "user_id": user_id} for e in events]
