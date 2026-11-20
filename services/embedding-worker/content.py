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
)


def _names(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def item_text(metadata: dict) -> str:
    """The text blob an item is embedded on. People and studios are written out as phrases
    ("Directed by Denis Villeneuve") so the model places films by the same director or with
    the same cast near each other, which is what "Because you watched" leans on.
    """
    parts = [
        metadata.get("title", ""),
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
#   - rated / favorited are states: only the latest one counts;
#   - completed counts once per item per day, played once per item per day (the strongest);
#   - weightless actions (cataloged, impression, paused) never count;
#   - a co-viewer's events (metadata.co_viewer: they were in the room, someone else chose)
#     count at CO_VIEWER_FACTOR.
STATE_ACTIONS = {"rated", "favorited"}
DAILY_ACTIONS = {"completed", "played", "skipped", "trailer_played", "detail_viewed"}
CO_VIEWER_FACTOR = 0.6


def collapse_events(rows: list[dict]) -> list[tuple[str, str, str, float, object]]:
    """rows: dicts with source, source_item_id, item_type, action, action_weight, timestamp,
    co_viewer. Returns (source, source_item_id, item_type, weight, timestamp) entries."""
    latest_state: dict[tuple, dict] = {}
    daily: dict[tuple, dict] = {}
    other: list[dict] = []
    for r in rows:
        w = r["action_weight"] or 0.0
        if w == 0:
            continue
        item = (r["source"], r["source_item_id"])
        if r["action"] in STATE_ACTIONS:
            k = (*item, r["action"])
            if k not in latest_state or r["timestamp"] > latest_state[k]["timestamp"]:
                latest_state[k] = r
        elif r["action"] in DAILY_ACTIONS:
            k = (*item, r["action"], r["timestamp"].date())
            if k not in daily or abs(w) > abs(daily[k]["action_weight"]):
                daily[k] = r
        else:
            other.append(r)
    out = []
    for r in [*latest_state.values(), *daily.values(), *other]:
        w = r["action_weight"] * (CO_VIEWER_FACTOR if r.get("co_viewer") else 1.0)
        out.append((r["source"], r["source_item_id"], r["item_type"], w, r["timestamp"]))
    return out
