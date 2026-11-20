"""Shared event model. Every adapter builds an `Event` and POSTs it to the ingestion API —
this is the one contract all adapters and the ingestion API must agree on.
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class Source(str, Enum):
    JELLYFIN = "jellyfin"
    ARR = "arr"
    FREETUBE = "freetube"
    SEARXNG = "searxng"
    OPENWEBUI = "openwebui"
    NAVIDROME = "navidrome"
    AUDIOBOOKSHELF = "audiobookshelf"
    MONO = "mono"  # Mono, the self-hosted music app (madmaxvoltro/mono)
    VESPER = "vesper"  # reserved; Vesper sends video events as "jellyfin" so ids join (docs/vesper.md)


class ItemType(str, Enum):
    MOVIE = "movie"
    SERIES_EPISODE = "series_episode"
    SERIES = "series"  # a show as a whole (thumbs, favourites, detail views on the series)
    YOUTUBE_VIDEO = "youtube_video"
    TRACK = "track"
    AUDIOBOOK = "audiobook"
    EBOOK = "ebook"
    SEARCH_QUERY = "search_query"
    CHAT = "chat"


class Action(str, Enum):
    PLAYED = "played"
    COMPLETED = "completed"
    PAUSED = "paused"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAVORITED = "favorited"
    SEARCHED = "searched"
    ADDED_WATCHLIST = "added_watchlist"
    REMOVED = "removed"
    DETAIL_VIEWED = "detail_viewed"  # opened a title's detail page (weak interest)
    TRAILER_PLAYED = "trailer_played"
    REQUESTED = "requested"  # asked for a title to be added to the library
    RATED = "rated"  # explicit thumbs (Vesper, via the Jellyfin user rating); metadata.value -1/1/2
    IMPRESSION = "impression"  # shown in a recommendation row; weight 0, feeds fatigue (shown often, never picked)
    CATALOGED = "cataloged"  # item exists in a library, no user signal attached — for backfilling
    # content embeddings from a library the adapter didn't watch the user acquire (see jellyfin adapter).


# Default action_weight per action — adapters may override, but this keeps
# negative signals (skipped/rejected) first-class and consistent by default.
DEFAULT_ACTION_WEIGHTS: dict[Action, float] = {
    Action.PLAYED: 0.3,
    Action.COMPLETED: 1.0,
    Action.PAUSED: 0.0,
    Action.SKIPPED: -0.5,
    Action.REJECTED: -1.0,
    Action.FAVORITED: 1.5,
    Action.SEARCHED: 0.2,
    Action.ADDED_WATCHLIST: 0.4,
    Action.REMOVED: -0.3,
    Action.DETAIL_VIEWED: 0.05,
    Action.TRAILER_PLAYED: 0.15,
    Action.REQUESTED: 0.6,
    Action.RATED: 1.0,  # adapters set the real weight from metadata.value, see RATED_WEIGHTS
    Action.IMPRESSION: 0.0,  # never part of the taste vector; read by the recommendation API's fatigue term
    Action.CATALOGED: 0.0,  # deliberately zero: skipped by the embedding worker's taste-vector
    # calc (see services/embedding-worker/worker.py decay_weight/recompute_taste_vectors), but
    # still lets the item get a content embedding so it's a valid recommendation candidate.
}


# Metadata keys with a shared meaning across sources (all optional):
#   title, description, genres, year, directors, cast, studios, tagline, official_rating,
#   runtime_minutes, community_rating (0-10), added_at, tmdb_id, tvdb_id, imdb_id,
#   series_id, series_name, season, episode       -- what the item is (merged into Qdrant)
#   value (rated: -1/0/1/2), query (searched), dedupe_key, via
#   from          -- where the action started: "row:<row id>", "search", "more_like_this:<id>"
#   together      -- other user_ids watching at the same time (co-viewing, see docs/vesper.md)
#   watched_ms    -- actual time watched in this session
#   original_user_id -- set by Muse when an event was moved onto a linked identity
# State-like actions (rated, favorited) count once per item: only the latest one matters.


class Event(BaseModel):
    user_id: UUID
    source: Source
    source_item_id: str | None = None
    item_type: ItemType
    action: Action
    action_weight: float | None = None  # if None, ingestion API fills in DEFAULT_ACTION_WEIGHTS
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    duration_ms: int | None = None
    progress_pct: float | None = Field(default=None, ge=0, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    device_context: dict[str, Any] | None = None


# Thumbs → action_weight for `rated` events. A thumbs down weighs like `rejected`, "love this"
# a bit more than `favorited`; 0 (rating cleared) cancels nothing retroactively but is recorded.
RATED_WEIGHTS: dict[int, float] = {-1: -1.0, 0: 0.0, 1: 1.0, 2: 2.0}
