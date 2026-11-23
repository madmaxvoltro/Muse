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
    MONO = "mono"  # self-hosted music server (github.com/madmaxvoltro/mono), item_type "track"
    # Muse itself: answers to its questions (onboarding, this-or-that, abandon reasons) and
    # things an assistant told it (query API / MCP)
    MUSE = "muse"
    CATALOG = "catalog"  # music that exists outside the library (MusicBrainz/ListenBrainz/Deezer, services/catalog)
    APP = "app"  # third-party apps writing events with an app token (metadata.app names the app)
    # History imports: not playable here, but real taste. Written through the ingestion API by a
    # key whose source is `import` (muse_taste.importers), or by the query API's
    # /v1/users/{u}/imports; either may claim exactly these sources and nothing else.
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"
    SOUNDCLOUD = "soundcloud"
    GOODREADS = "goodreads"
    STORYGRAPH = "storygraph"
    LETTERBOXD = "letterboxd"
    IMDB = "imdb"
    NETFLIX = "netflix"
    TRAKT = "trakt"


IMPORT_SOURCES: frozenset[str] = frozenset(
    {Source.YOUTUBE, Source.SPOTIFY, Source.SOUNDCLOUD, Source.GOODREADS, Source.STORYGRAPH, Source.LETTERBOXD,
     Source.IMDB, Source.NETFLIX, Source.TRAKT}
)


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
    CATALOGED = "cataloged"  # item exists in a library, no user signal attached — for backfilling
    # content embeddings from a library the adapter didn't watch the user acquire (see jellyfin adapter).
    UNFAVORITED = "unfavorited"  # took a favourite back; cancels the favourite's pull on the taste vector
    ADDED_TO_PLAYLIST = "added_to_playlist"  # deliberate curation, stronger than a single play
    # Elicitation answers (source=muse, see services/ingestion-api /answers):
    CHOSE = "chose"  # picked this item in a this-or-that question
    PASSED_OVER = "passed_over"  # the other side of a this-or-that question
    ABANDON_REASON = "abandon_reason"  # why a title was stopped early; metadata.reason
    INTEREST_FEEDBACK = "interest_feedback"  # answer about a whole interest (more/less/bring back...); metadata.response
    REASON_FEEDBACK = "reason_feedback"  # correction to a reason Muse gave (don't use this title / more or less of this tag)


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
    Action.UNFAVORITED: -1.5,  # exactly undoes FAVORITED in the taste vector's weighted sum
    Action.ADDED_TO_PLAYLIST: 0.8,
    Action.CHOSE: 0.5,
    Action.PASSED_OVER: -0.1,  # relative, not a dislike: the other option was merely preferred
    Action.ABANDON_REASON: 0.0,  # interpreted by muse_taste.signals from metadata.reason
    Action.INTEREST_FEEDBACK: 0.0,  # interpreted by muse_taste.evolution, never a per-title signal
    Action.REASON_FEEDBACK: 0.0,  # interpreted by muse_taste.signals.feedback_overrides
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


# Thumbs → action_weight for `rated` events. A thumbs down weighs like `rejected`, "love this"
# a bit more than `favorited`; 0 (rating cleared) cancels nothing retroactively but is recorded.
RATED_WEIGHTS: dict[int, float] = {-1: -1.0, 0: 0.0, 1: 1.0, 2: 2.0}
