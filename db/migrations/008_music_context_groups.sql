-- Music beyond the library (catalog from public databases, new releases, wanted list), audio
-- analysis, impressions and a learned ranker, context and moods, group sessions, goals,
-- webhooks, privacy controls. Safe to re-run; apply by hand on existing databases:
--   docker compose exec -T postgres psql -U muse -d muse < db/migrations/008_music_context_groups.sql

-- ---------------------------------------------------------------------------------------------
-- Audio analysis (services/audio-analyzer). One row per analysed item; the embedding itself
-- lives in Qdrant (collection audio_items), this row holds the readable features.
CREATE TABLE IF NOT EXISTS item_audio (
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed', 'unavailable')),
    features        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- bpm, key, mode, camelot, energy, loudness_db, brightness, danceability, acousticness, intro{}, outro{}
    has_embedding   BOOLEAN NOT NULL DEFAULT FALSE,
    model           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    analyzed_at     TIMESTAMPTZ,
    PRIMARY KEY (source, source_item_id)
);
CREATE INDEX IF NOT EXISTS idx_item_audio_pending ON item_audio (attempts) WHERE status = 'pending';

-- ---------------------------------------------------------------------------------------------
-- Catalog: music that exists, not only what's in the library (services/catalog).
CREATE TABLE IF NOT EXISTS catalog_artist (
    mbid                TEXT PRIMARY KEY,            -- MusicBrainz artist id
    name                TEXT NOT NULL,
    country             TEXT,
    tags                JSONB NOT NULL DEFAULT '[]'::jsonb,   -- MusicBrainz genres/tags, most-voted first
    deezer_id           BIGINT,
    related             JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{mbid?, name, score, via}]
    seed_weight         REAL NOT NULL DEFAULT 0,     -- how much the household loves this artist (0 = only discovered)
    fetched_at          TIMESTAMPTZ,
    tracks_fetched_at   TIMESTAMPTZ,
    releases_checked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_catalog_artist_seed ON catalog_artist (seed_weight DESC);

-- Artist name -> MusicBrainz id resolution cache, including misses (mbid NULL).
CREATE TABLE IF NOT EXISTS catalog_name (
    name_key        TEXT PRIMARY KEY,                -- normalised artist name
    mbid            TEXT,
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Release groups (albums, EPs, singles) of artists Muse follows; first_seen_at drives "new release".
CREATE TABLE IF NOT EXISTS catalog_release (
    mbid            TEXT PRIMARY KEY,                -- MusicBrainz release-group id
    artist_mbid     TEXT NOT NULL,
    artist_name     TEXT NOT NULL,
    title           TEXT NOT NULL,
    primary_type    TEXT,                            -- Album | Single | EP | ...
    secondary_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    release_date    DATE,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_catalog_release_artist ON catalog_release (artist_mbid, release_date DESC);

-- What a person (or the household) wants acquired: for a music-Radarr, Lidarr import lists,
-- webhooks. One row per (user, dedupe_key).
CREATE TABLE IF NOT EXISTS wanted (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,                            -- canonical person; NULL = household
    kind            TEXT NOT NULL CHECK (kind IN ('release', 'recording', 'artist')),
    mbid            TEXT,                            -- release-group / recording / artist mbid when known
    artist_mbid     TEXT,
    artist_name     TEXT NOT NULL,
    title           TEXT,
    release_date    DATE,
    reason          TEXT,
    score           REAL NOT NULL DEFAULT 0.5,
    origin          TEXT NOT NULL,                   -- new_release | discovery | wishlist | assistant | user
    status          TEXT NOT NULL DEFAULT 'wanted' CHECK (status IN ('wanted', 'grabbed', 'in_library', 'dismissed', 'failed')),
    external_id     TEXT,                            -- the acquiring tool's own id
    dedupe_key      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (user_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_wanted_status ON wanted (status, score DESC);

-- ---------------------------------------------------------------------------------------------
-- Impressions: what Muse showed, to whom, and what came of it. The base for evaluation,
-- "shown but ignored" demotion and the learned ranker.
CREATE TABLE IF NOT EXISTS impression (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID NOT NULL,
    user_id         UUID NOT NULL,                   -- canonical
    item_type       TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    position        INTEGER NOT NULL,
    score           REAL,
    explore         BOOLEAN NOT NULL DEFAULT FALSE,
    generator       TEXT,                            -- which candidate generator produced it
    surface         TEXT,                            -- recommendations | radio | next | group | search ...
    features        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- the ranking features, for training
    shown_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome         TEXT,                            -- positive | negative | none (set by the trainer)
    outcome_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_impression_user_item ON impression (user_id, source, source_item_id);
CREATE INDEX IF NOT EXISTS idx_impression_shown ON impression (shown_at);

-- Ranker weights learned from impressions (logistic regression over the scoring features).
CREATE TABLE IF NOT EXISTS ranker_model (
    id              SERIAL PRIMARY KEY,
    item_type       TEXT NOT NULL,                   -- or '_all'
    weights         JSONB NOT NULL,                  -- {"bias": b, "similarity": w, ...}
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- holdout AUC of learned vs hand weights
    samples         INTEGER NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT FALSE,  -- only when it beat the hand weights on holdout
    trained_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Context: what's going on right now, pushed by apps, assistants, Home Assistant, or fetched
-- (weather, calendar). Scoped to a person, a room, or the whole household.
CREATE TABLE IF NOT EXISTS context_signal (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,                            -- canonical person; NULL for room/household signals
    room            TEXT,                            -- e.g. 'living room'; NULL = not room-bound
    kind            TEXT NOT NULL,                   -- mood | activity | presence | device | weather | calendar | location | custom
    value           JSONB NOT NULL,
    source          TEXT NOT NULL,                   -- app name, 'assistant', 'home_assistant', 'weather', 'calendar'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_context_active ON context_signal (user_id, kind, created_at DESC);

-- Calendars (ICS URLs) per person, polled for "what kind of day is it".
CREATE TABLE IF NOT EXISTS calendar_feed (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    url             TEXT NOT NULL,                   -- secret-ish: an ICS URL often carries a token
    label           TEXT,
    last_fetched_at TIMESTAMPTZ,
    last_error      TEXT
);

-- ---------------------------------------------------------------------------------------------
-- Group sessions: several can run at once (A+B in the living room, C+K upstairs).
CREATE TABLE IF NOT EXISTS group_session (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT,
    room            TEXT,
    device          TEXT,
    members         UUID[] NOT NULL,                 -- canonical ids
    origin          TEXT NOT NULL DEFAULT 'api',     -- app | home_assistant | api | assistant
    created_by      TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_group_session_active ON group_session (started_at DESC) WHERE ended_at IS NULL;

-- What a group ended up choosing, with how well it fitted each member: fairness memory, so the
-- member who keeps compromising gets more weight next time.
CREATE TABLE IF NOT EXISTS group_pick (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID,
    members         UUID[] NOT NULL,
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    item_type       TEXT NOT NULL,
    fits            JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {user_id: 0..1}
    picked_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Goals: "explore jazz", "watch 10 classics this year".
CREATE TABLE IF NOT EXISTS goal (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    domain          TEXT NOT NULL DEFAULT 'general',
    criteria        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"genres": [], "artists": [], "text": "..."}
    target          INTEGER NOT NULL DEFAULT 10,     -- items to complete
    progress        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done', 'dropped')),
    created_by      TEXT NOT NULL DEFAULT 'user',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_at          TIMESTAMPTZ,
    done_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_goal_user ON goal (user_id) WHERE status = 'active';

-- ---------------------------------------------------------------------------------------------
-- Webhooks and their delivery queue (services/notifier).
CREATE TABLE IF NOT EXISTS webhook (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,                            -- NULL = household-level hook (admin only)
    url             TEXT NOT NULL,
    secret          TEXT NOT NULL,                   -- HMAC-SHA256 signing key, shown once
    events          TEXT[] NOT NULL,                 -- new_release | wanted | digest | question | goal | recommendation | *
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    failures        INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS webhook_delivery (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_id      UUID NOT NULL REFERENCES webhook(id) ON DELETE CASCADE,
    event           TEXT NOT NULL,
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at    TIMESTAMPTZ,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_delivery_due ON webhook_delivery (next_attempt_at) WHERE delivered_at IS NULL;

-- Notifications Muse has already raised, so a new release or digest is announced once.
CREATE TABLE IF NOT EXISTS notification_log (
    key             TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Privacy and access.
CREATE TABLE IF NOT EXISTS access_log (
    id              BIGSERIAL PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    principal       TEXT NOT NULL,                   -- 'admin' | 'user' | 'adapter:<source>'
    token_id        UUID,
    actor_user_id   UUID,                            -- whose token
    subject_user_id UUID,                            -- whose data
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    status          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_log_subject ON access_log (subject_user_id, at DESC);

-- How long events from each source are kept (the embedding worker enforces it).
CREATE TABLE IF NOT EXISTS retention_policy (
    source          TEXT PRIMARY KEY,
    keep_days       INTEGER NOT NULL CHECK (keep_days > 0)
);

-- History imports (Spotify, YouTube, Goodreads, StoryGraph, SoundCloud).
CREATE TABLE IF NOT EXISTS import_job (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'done',
    stats           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE api_token ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE api_token ADD COLUMN IF NOT EXISTS app_source TEXT;  -- apps that write events: their source name
