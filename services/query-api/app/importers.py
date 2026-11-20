"""History imports: years of taste from other services, as Muse events.

Each parser turns an export file into event dicts (no I/O here). Every event gets a dedupe_key
derived from the service's own data, so importing the same export twice changes nothing.

* spotify    — "Extended streaming history" (Streaming_History_Audio_*.json; also the older
               StreamingHistory*.json) and YourLibrary.json (liked songs).
               Finished -> completed, skipped / < 30 s -> skipped, else played.
* youtube    — Google Takeout watch-history.json (ads are dropped).
* goodreads  — library export CSV: stars -> rating, to-read -> watchlist, read -> completed.
* storygraph — export CSV: same idea, did-not-finish -> skipped.
* soundcloud — likes/history as CSV (title, artist[, date]) or JSON.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterator

KINDS = ("spotify", "youtube", "goodreads", "storygraph", "soundcloud")


def _key(*parts: str | None) -> str:
    s = "|".join(p or "" for p in parts)
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9|]+", "", "".join(ch for ch in s if not unicodedata.combining(ch)).casefold())


def _ts(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    s = str(v).strip().replace(" UTC", "Z")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ev(item_type: str, item_id: str, action: str, at: datetime | None, md: dict, dedupe: str, weight: float | None = None,
        progress: float | None = None, duration_ms: int | None = None) -> dict:
    return {"item_type": item_type, "source_item_id": item_id[:300], "action": action, "timestamp": at or datetime.now(timezone.utc),
            "metadata": {**{k: v for k, v in md.items() if v not in (None, "", [])}, "dedupe_key": dedupe, "imported": True},
            "action_weight": weight, "progress_pct": progress, "duration_ms": duration_ms}


# --- Spotify ------------------------------------------------------------------------------------------


def spotify(name: str, raw: bytes) -> Iterator[dict]:
    data = json.loads(raw.decode("utf-8-sig"))
    if isinstance(data, dict) and "tracks" in data:  # YourLibrary.json: liked songs
        for t in data.get("tracks", []):
            artist, title = t.get("artist", ""), t.get("track", "")
            if not (artist and title):
                continue
            uri = t.get("uri") or _key(artist, title)
            yield _ev("track", uri, "favorited", None, {"title": f"{artist} - {title}", "artist": artist, "album": t.get("album")},
                      f"spotify:like:{uri}")
        return
    for r in data if isinstance(data, list) else []:
        artist = r.get("master_metadata_album_artist_name") or r.get("artistName")
        title = r.get("master_metadata_track_name") or r.get("trackName")
        if not (artist and title):
            continue  # podcasts, audiobooks, unknown
        ms = int(r.get("ms_played") or r.get("msPlayed") or 0)
        at = _ts(r.get("ts") or r.get("endTime"))
        uri = r.get("spotify_track_uri") or _key(artist, title)
        if r.get("skipped") or ms < 30_000:
            action = "skipped"
        elif r.get("reason_end") == "trackdone":
            action = "completed"
        else:
            action = "played"
        yield _ev("track", uri, action, at, {"title": f"{artist} - {title}", "artist": artist, "album": r.get("master_metadata_album_album_name"),
                                              "platform": r.get("platform")}, f"spotify:{at.isoformat() if at else ''}:{uri}", duration_ms=ms)


# --- YouTube ---------------------------------------------------------------------------------------------

_VID = re.compile(r"[?&]v=([\w-]{6,})")


def youtube(name: str, raw: bytes) -> Iterator[dict]:
    for r in json.loads(raw.decode("utf-8-sig")):
        if any((d.get("name") or "").startswith("From Google Ads") for d in r.get("details", []) or []):
            continue
        m = _VID.search(r.get("titleUrl") or "")
        if not m:
            continue
        title = re.sub(r"^(Watched|Bekeken|Je hebt|Visto)\s+", "", r.get("title") or "").strip()
        channel = ((r.get("subtitles") or [{}])[0]).get("name")
        at = _ts(r.get("time"))
        yield _ev("youtube_video", m.group(1), "played", at, {"title": title, "channel": channel, "description": f"YouTube video by {channel}" if channel else None},
                  f"youtube:{at.isoformat() if at else ''}:{m.group(1)}")


# --- books -----------------------------------------------------------------------------------------------

STARS = {5: 2.0, 4: 1.0, 3: 0.3, 2: -0.5, 1: -1.0}


def _rows(raw: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def goodreads(name: str, raw: bytes) -> Iterator[dict]:
    for r in _rows(raw):
        title, author = (r.get("Title") or "").strip(), (r.get("Author") or "").strip()
        if not title:
            continue
        bid = r.get("Book Id") or _key(author, title)
        md = {"title": title, "author": author, "isbn": (r.get("ISBN13") or "").strip('="'), "description": f"Book by {author}" if author else None,
              "genres": [s.strip() for s in (r.get("Bookshelves") or "").split(",") if s.strip() and s.strip() not in ("to-read", "currently-reading", "read")]}
        shelf = (r.get("Exclusive Shelf") or "").strip()
        stars = int(float(r.get("My Rating") or 0))
        if shelf == "to-read":
            yield _ev("ebook", f"goodreads:{bid}", "added_watchlist", _ts(r.get("Date Added")), md, f"goodreads:{bid}:want")
        elif stars:
            yield _ev("ebook", f"goodreads:{bid}", "rated", _ts(r.get("Date Read") or r.get("Date Added")), {**md, "value": stars},
                      f"goodreads:{bid}:rated", weight=STARS[stars])
        elif shelf == "read":
            yield _ev("ebook", f"goodreads:{bid}", "completed", _ts(r.get("Date Read") or r.get("Date Added")), md, f"goodreads:{bid}:read")
        elif shelf == "currently-reading":
            yield _ev("ebook", f"goodreads:{bid}", "played", _ts(r.get("Date Added")), md, f"goodreads:{bid}:reading")


def storygraph(name: str, raw: bytes) -> Iterator[dict]:
    for r in _rows(raw):
        title, author = (r.get("Title") or "").strip(), (r.get("Authors") or "").strip()
        if not title:
            continue
        bid = r.get("ISBN/UID") or _key(author, title)
        md = {"title": title, "author": author, "description": f"Book by {author}" if author else None,
              "genres": [s.strip() for s in (r.get("Tags") or "").split(",") if s.strip()], "moods": r.get("Moods")}
        status = (r.get("Read Status") or "").strip().lower()
        stars = float(r.get("Star Rating") or 0)
        at = _ts(r.get("Last Date Read") or r.get("Dates Read") or r.get("Date Added"))
        if status == "to-read":
            yield _ev("ebook", f"storygraph:{bid}", "added_watchlist", at, md, f"storygraph:{bid}:want")
        elif status == "did-not-finish":
            yield _ev("ebook", f"storygraph:{bid}", "skipped", at, md, f"storygraph:{bid}:dnf")
        elif stars:
            s = int(round(stars))
            yield _ev("ebook", f"storygraph:{bid}", "rated", at, {**md, "value": stars}, f"storygraph:{bid}:rated", weight=STARS[max(1, min(5, s))])
        elif status == "read":
            yield _ev("ebook", f"storygraph:{bid}", "completed", at, md, f"storygraph:{bid}:read")


# --- SoundCloud ------------------------------------------------------------------------------------------


def soundcloud(name: str, raw: bytes) -> Iterator[dict]:
    text = raw.decode("utf-8-sig").strip()
    rows: list[dict] = json.loads(text) if text[:1] in "[{" else _rows(raw)
    if isinstance(rows, dict):
        rows = rows.get("collection") or rows.get("likes") or []
    for r in rows:
        low = {str(k).lower().strip(): v for k, v in r.items()}
        track = low.get("track") if isinstance(low.get("track"), dict) else {}
        title = (low.get("title") or low.get("track title") or track.get("title") or "").strip()
        artist = (low.get("artist") or low.get("creator") or low.get("user") or (track.get("user") or {}).get("username") or "").strip()
        if isinstance(artist, dict):
            artist = artist.get("username", "")
        if not title:
            continue
        url = low.get("url") or low.get("permalink_url") or track.get("permalink_url") or ""
        tid = url or _key(artist, title)
        at = _ts(low.get("created_at") or low.get("date") or low.get("liked_at"))
        action = "played" if "history" in name.lower() else "favorited"
        yield _ev("track", tid, action, at, {"title": f"{artist} - {title}" if artist else title, "artist": artist},
                  f"soundcloud:{action}:{tid}:{at.date().isoformat() if at and action == 'played' else ''}")


PARSERS = {"spotify": spotify, "youtube": youtube, "goodreads": goodreads, "storygraph": storygraph, "soundcloud": soundcloud}


MAX_UNZIPPED = 1_000_000_000  # zip bombs: refuse archives that expand beyond 1 GB of JSON/CSV
MAX_FILES = 2000


def parse(kind: str, files: list[tuple[str, bytes]], _depth: int = 0) -> list[dict]:
    out: list[dict] = []
    for name, raw in files:
        if name.lower().endswith(".zip"):
            import zipfile

            if _depth > 0:
                continue  # no zips inside zips
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                infos = [i for i in z.infolist() if i.filename.lower().endswith((".json", ".csv"))]
                if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
                    raise ValueError("archive too large once unpacked")
                inner = [(i.filename, z.read(i)) for i in infos]
            out += parse(kind, inner, _depth + 1)
            continue
        if kind == "spotify" and not re.search(r"(stream|history|library)", name, re.I):
            continue  # other files in a Spotify export (userdata, inferences...) aren't taste
        out += list(PARSERS[kind](name, raw))
    return out
