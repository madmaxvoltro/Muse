"""Canonical item identity.

The same film shows up as a Jellyfin GUID, a Letterboxd slug, an IMDb `tt` id, a Netflix title
string and a TMDB id. Taste must be computed per *real-world title*, or a five-star Letterboxd
rating never reaches the copy in Jellyfin and a film watched on Netflix gets recommended again.

A canonical key is the strongest identifier known for an item, in this order:
  tmdb:movie:603 / tmdb:tv:1399        (TMDB is what enrichment and the curator speak)
  imdb:tt0133093 / tvdb:121361         (upgraded to tmdb:* once the resolver has looked it up)
  yt:dQw4w9WgXcQ / ytchannel:UC...     (YouTube)
  track:<artist>|<title>               (normalised, so Spotify, SoundCloud and Navidrome agree)
  book:isbn:<isbn> / book:<author>|<title>
  <source>:<source_item_id>            (fallback: no shared identifier yet)
"""

from __future__ import annotations

import re
import unicodedata

VIDEO_TYPES = frozenset({"movie", "series", "series_episode"})

# Each item_type belongs to one taste *domain*. Profiles are built per domain, so a film you
# loved informs series recommendations (same medium, same taste) while music never leaks into
# film recommendations.
DOMAIN_OF: dict[str, str] = {
    "movie": "video",
    "series": "video",
    "series_episode": "video",
    "youtube_video": "youtube",
    "track": "music",
    "audiobook": "books",
    "ebook": "books",
}

_TMDB_KIND = {"movie": "movie", "series": "tv", "series_episode": "tv"}

_FEAT_RE = re.compile(r"\s*[\(\[](feat\.?|ft\.?|with|prod\.?)[^\)\]]*[\)\]]", re.IGNORECASE)
_SUFFIX_RE = re.compile(
    r"\s+-\s+(\d{4}\s+)?(remaster(ed)?|radio edit|single version|album version|mono|stereo|live|explicit)\b.*$",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def domain_of(item_type: str) -> str:
    return DOMAIN_OF.get(item_type, item_type)


def norm_text(text: str | None) -> str:
    """Lowercase ASCII-folded alphanumerics, single spaces: 'Beyoncé – Halo!' -> 'beyonce halo'."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_WORD.sub(" ", folded.lower()).strip()


def norm_track_title(title: str | None) -> str:
    if not title:
        return ""
    t = _FEAT_RE.sub("", title)
    t = _SUFFIX_RE.sub("", t)
    return norm_text(t)


def norm_artist(artist: str | None) -> str:
    if not artist:
        return ""
    a = re.sub(r"\s+-\s+topic$", "", artist.strip(), flags=re.IGNORECASE)  # YouTube Music channels
    a = re.split(r"\s*(,|&| feat\.? | ft\.? | x )\s*", a, maxsplit=1, flags=re.IGNORECASE)[0]
    a = re.sub(r"^the\s+", "", a.strip(), flags=re.IGNORECASE)
    return norm_text(a)


def external_ids(metadata: dict) -> dict[str, str]:
    """Collects ids from the shapes adapters send: metadata.external_ids (preferred),
    Jellyfin ProviderIds, or flat tmdb_id/imdb_id fields."""
    out: dict[str, str] = {}
    for src in (metadata.get("provider_ids") or {}, metadata.get("external_ids") or {}):
        for k, v in src.items():
            if v not in (None, ""):
                out[k.lower()] = str(v)
    for k in ("tmdb", "imdb", "tvdb"):
        v = metadata.get(f"{k}_id")
        if v not in (None, "") and k not in out:
            out[k] = str(v)
    return out


def canon_key(source: str, source_item_id: str | None, item_type: str, metadata: dict | None) -> str:
    md = metadata or {}
    if md.get("canon_key"):
        return str(md["canon_key"])  # Muse's own events (answers) reference canonical titles directly
    ids = external_ids(md)
    kind = _TMDB_KIND.get(item_type)

    if kind is not None:
        if item_type == "series_episode":
            # An episode's own identity is rarely useful for taste; parent_canon_key() rolls
            # it up. Episodes still get a stable key of their own for dedupe.
            if ids.get("tmdb"):
                return f"tmdb:episode:{ids['tmdb']}"
            if ids.get("imdb"):
                return f"imdb:{ids['imdb']}"
        else:
            if ids.get("tmdb"):
                return f"tmdb:{kind}:{ids['tmdb']}"
            if ids.get("imdb"):
                return f"imdb:{ids['imdb']}"
            if ids.get("tvdb") and kind == "tv":
                return f"tvdb:{ids['tvdb']}"

    if item_type == "youtube_video":
        vid = md.get("video_id") or (source_item_id if source in ("youtube", "freetube") else None)
        if md.get("kind") == "channel" and md.get("channel_id"):
            return f"ytchannel:{md['channel_id']}"
        if vid:
            return f"yt:{vid}"

    if item_type == "track":
        artist = norm_artist(md.get("artist") or md.get("album_artist"))
        title = norm_track_title(md.get("title"))
        if artist and title:
            return f"track:{artist}|{title}"

    if item_type in ("audiobook", "ebook"):
        if ids.get("isbn"):
            return f"book:isbn:{ids['isbn']}"
        author = norm_text(md.get("author"))
        title = norm_text(md.get("title"))
        if author and title:
            return f"book:{author}|{title}"

    return f"{source}:{source_item_id}"


def parent_canon_key(source: str, item_type: str, metadata: dict | None) -> str | None:
    """For an episode: the series it belongs to, which is where taste lives (a 60-episode
    binge is one strong signal about one show, not 60 signals). None for anything else."""
    if item_type != "series_episode":
        return None
    md = metadata or {}
    sids = {k.lower(): str(v) for k, v in (md.get("series_external_ids") or md.get("series_provider_ids") or {}).items() if v}
    if sids.get("tmdb"):
        return f"tmdb:tv:{sids['tmdb']}"
    if sids.get("imdb"):
        return f"imdb:{sids['imdb']}"
    if sids.get("tvdb"):
        return f"tvdb:{sids['tvdb']}"
    if md.get("series_id"):
        return f"{source}:{md['series_id']}"  # the Jellyfin series item; resolved to tmdb later
    if md.get("series_title"):
        return f"title:tv:{norm_text(md['series_title'])}"
    return None


def is_weak_key(key: str) -> bool:
    """True while a key is still a placeholder the resolver should try to upgrade."""
    return not key.startswith(("tmdb:", "yt:", "ytchannel:", "track:", "book:"))
