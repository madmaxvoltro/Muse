-- Muse event store: single append-only table all adapters write into.
-- Requires the TimescaleDB extension (image: timescale/timescaledb:latest-pg16).

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS event (
    id              UUID            NOT NULL DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL,
    source          TEXT            NOT NULL,   -- 'jellyfin' | 'arr' | 'freetube' | 'searxng' | 'openwebui' | 'navidrome' | 'audiobookshelf'
    source_item_id  TEXT,
    item_type       TEXT            NOT NULL,   -- 'movie' | 'series_episode' | 'youtube_video' | 'track' | 'audiobook' | 'ebook' | 'search_query' | 'chat'
    action          TEXT            NOT NULL,   -- 'played' | 'completed' | 'paused' | 'skipped' | 'rejected' | 'favorited' | 'searched' | 'added_watchlist' | 'removed'
    action_weight   REAL            NOT NULL DEFAULT 0,
    "timestamp"     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    duration_ms     INTEGER,
    progress_pct    REAL,
    metadata        JSONB           NOT NULL DEFAULT '{}'::jsonb,
    device_context  JSONB,
    PRIMARY KEY (id, "timestamp")
);

-- Hypertable partitioned on time — this is what TimescaleDB is for (decay/rolling-window queries).
SELECT create_hypertable('event', by_range('timestamp'), if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_event_user_time ON event (user_id, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_event_source ON event (source, "timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_event_item ON event (source, source_item_id);
CREATE INDEX IF NOT EXISTS idx_event_action ON event (action);

-- Identity resolution: maps a source-native username to a Muse user_id.
CREATE TABLE IF NOT EXISTS identity_mapping (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL,
    source          TEXT            NOT NULL,
    source_username TEXT            NOT NULL,
    confirmed       BOOLEAN         NOT NULL DEFAULT FALSE,  -- fuzzy matches start unconfirmed, never auto-committed
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (source, source_username)
);

-- User-tunable overlay controls (sliders, explore-dial, pins) — never mutates the behavior-derived taste vector itself.
CREATE TABLE IF NOT EXISTS user_overlay (
    user_id             UUID PRIMARY KEY,
    source_weights      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- e.g. {"searxng": 0.2, "jellyfin": 1.0}
    explore_ratio       REAL NOT NULL DEFAULT 0.125,          -- 10-15% default
    pinned_topics        JSONB NOT NULL DEFAULT '[]'::jsonb,
    disabled_sources     JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Curator: tracks "Soon Gone" removal candidates and their grace window.
CREATE TABLE IF NOT EXISTS soon_gone (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_item_id  TEXT NOT NULL,
    item_type       TEXT NOT NULL,
    marked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,       -- marked_at + 48h
    whitelisted     BOOLEAN NOT NULL DEFAULT FALSE,
    whitelisted_by  UUID,                        -- user_id of whoever saved it, null until whitelisted
    removed         BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_soon_gone_pending ON soon_gone (expires_at) WHERE whitelisted = FALSE AND removed = FALSE;
