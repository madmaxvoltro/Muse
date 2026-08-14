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


class ItemType(str, Enum):
    MOVIE = "movie"
    SERIES_EPISODE = "series_episode"
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
    Action.CATALOGED: 0.0,  # deliberately zero: skipped by the embedding worker's taste-vector
    # calc (see services/embedding-worker/worker.py decay_weight/recompute_taste_vectors), but
    # still lets the item get a content embedding so it's a valid recommendation candidate.
}


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
