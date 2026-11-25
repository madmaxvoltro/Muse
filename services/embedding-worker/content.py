"""Pure helpers for turning event metadata into embedding text and Qdrant payloads.
Kept free of model/DB imports so they are unit-testable (tests/test_content.py).
"""

from typing import Any

# Payload fields copied from event metadata onto the item's Qdrant point. The recommendation
# API reads these for filters (official_rating, runtime_minutes), explanations (directors,
# cast, genres), Soon Gone joins (tmdb_id/tvdb_id) and series roll-ups (series_id).
PAYLOAD_FIELDS = (
    "title",
    "genres",
    "year",
    "directors",
    "cast",
    "studios",
    "tagline",
    "official_rating",
    "runtime_minutes",
    "community_rating",
    "added_at",
    "tmdb_id",
    "tvdb_id",
    "imdb_id",
    "series_id",
    "series_name",
    "season",
    "episode",
    "artist",
    "album",
)


def normalize(metadata: dict) -> dict:
    """One shape for item metadata, whichever adapter wrote it. The Jellyfin adapter sends
    `people` [{name, type}], `provider_ids`, `taglines` and `runtime_min`; Vesper and older
    adapters send directors/cast lists, tmdb_id and runtime_minutes. Readers (embedding text,
    payload filters, "Why this") use the flat names."""
    md = dict(metadata or {})
    people = md.get("people") or []
    if people and not md.get("directors"):
        md["directors"] = [p["name"] for p in people if isinstance(p, dict) and p.get("type") == "Director" and p.get("name")][:3]
    if people and not md.get("cast"):
        md["cast"] = [p["name"] for p in people if isinstance(p, dict) and p.get("type") == "Actor" and p.get("name")][:6]
    ids = {str(k).lower(): v for k, v in (md.get("provider_ids") or {}).items() if v}
    for key in ("tmdb", "tvdb", "imdb"):
        if key in ids and not md.get(f"{key}_id"):
            md[f"{key}_id"] = str(ids[key])
    if md.get("taglines") and not md.get("tagline"):
        md["tagline"] = md["taglines"][0]
    if md.get("runtime_min") and not md.get("runtime_minutes"):
        md["runtime_minutes"] = md["runtime_min"]
    return {k: v for k, v in md.items() if v not in (None, "", [], {})}


def _names(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def item_text(metadata: dict) -> str:
    """Text an item is embedded on (after normalize()). The text blob an item is embedded on. People and studios are written out as phrases
    ("Directed by Denis Villeneuve") so the model places films by the same director or with
    the same cast near each other, which is what "Because you watched" leans on.
    """
    metadata = normalize(metadata)
    artist = metadata.get("artist", "")
    parts = [
        metadata.get("title", ""),
        artist if artist and artist not in metadata.get("title", "") else "",
        metadata.get("album", ""),
        metadata.get("series_name", "") if metadata.get("series_name") != metadata.get("title") else "",
        metadata.get("tagline", ""),
        metadata.get("description", ""),
        " ".join(_names(metadata.get("genres"))),
    ]
    if directors := _names(metadata.get("directors")):
        parts.append("Directed by " + ", ".join(directors))
    if cast := _names(metadata.get("cast")):
        parts.append("Starring " + ", ".join(cast[:4]))
    if studios := _names(metadata.get("studios")):
        parts.append("From " + ", ".join(studios[:2]))
    parts.append(metadata.get("query", ""))  # search_query items
    return " ".join(p.strip() for p in parts if p and str(p).strip()).strip()


# Per-event bookkeeping, not facts about the item.
EVENT_ONLY_KEYS = {"dedupe_key", "via", "value"}


def merge_metadata(previous: dict | None, new: dict) -> dict:
    """New non-empty values win; anything the new event lacks is kept from what was known.
    Vesper's detail_viewed events carry less than the Jellyfin adapter's catalog events, and
    must not wipe the directors or ratings those brought.
    """
    merged = dict(previous or {})
    for k, v in (new or {}).items():
        if v in (None, "", [], {}) or k in EVENT_ONLY_KEYS:
            continue
        merged[k] = v
    return merged


def build_payload(source: str, source_item_id: str, item_type: str, metadata: dict, previous: dict | None, first_seen: str) -> dict:
    metadata = normalize(metadata)
    payload: dict[str, Any] = {k: v for k, v in (previous or {}).items()}
    for k in PAYLOAD_FIELDS:
        if k in metadata and metadata[k] not in (None, "", []):
            payload[k] = metadata[k]
    payload.update({"source": source, "source_item_id": source_item_id, "item_type": item_type})
    payload.setdefault("title", "")
    payload.setdefault("genres", [])
    payload.setdefault("first_seen_at", first_seen)
    # The full metadata (description included) so later events can be merged without loss.
    payload["metadata"] = metadata
    return payload


# How events become taste weights. Several sources can report the same thing (Vesper sends a
# completion the moment the credits roll, the Jellyfin adapter sees it on its next poll), so
# events are collapsed per item before they count:
#   - rated is a state, favorited/unfavorited one state between them: only the latest counts
#     (an unfavorite as the latest leaves nothing);
#   - for films, series and books, completed, played, skipped, trailer_played and detail_viewed
#     count once per item per day (the strongest of that day); for music and short videos,
#     where replaying is itself a signal, only reports within REPORT_WINDOW of each other merge;
#   - weightless actions (cataloged, impression, paused) never count;
#   - a co-viewer's events (metadata.co_viewer: they were in the room, someone else chose)
#     count at CO_VIEWER_FACTOR.
STATE_GROUPS = {"rated": "rated", "favorited": "favorite", "unfavorited": "favorite"}
DAILY_ACTIONS = {"completed", "played", "skipped", "trailer_played", "detail_viewed"}
CO_VIEWER_FACTOR = 0.6
DAILY_TYPES = {"movie", "series", "series_episode", "audiobook", "ebook"}
REPORT_WINDOW_S = 600


def collapse_events(rows: list[dict]) -> list[tuple[dict, float]]:
    """rows: dicts with source, source_item_id, action, action_weight, timestamp and optionally
    co_viewer (any other fields are kept). Returns (row, weight) for the events that count."""
    latest_state: dict[tuple, dict] = {}
    daily: dict[tuple, dict] = {}
    other: list[dict] = []
    for r in rows:
        w = r["action_weight"] or 0.0
        item = (r["source"], r["source_item_id"])
        if r["action"] in STATE_GROUPS:
            k = (*item, STATE_GROUPS[r["action"]])
            if k not in latest_state or r["timestamp"] > latest_state[k]["timestamp"]:
                latest_state[k] = r
            continue
        if w == 0:
            continue
        if r["action"] in DAILY_ACTIONS:
            if r.get("item_type") in DAILY_TYPES:
                bucket = r["timestamp"].date()
            else:
                bucket = int(r["timestamp"].timestamp() // REPORT_WINDOW_S)
            k = (*item, r["action"], bucket)
            if k not in daily or abs(w) > abs(daily[k]["action_weight"]):
                daily[k] = r
        else:
            other.append(r)
    out = []
    for r in [*latest_state.values(), *daily.values(), *other]:
        if r["action"] == "unfavorited" or not r["action_weight"]:
            continue
        out.append((r, r["action_weight"] * (CO_VIEWER_FACTOR if r.get("co_viewer") else 1.0)))
    return out
