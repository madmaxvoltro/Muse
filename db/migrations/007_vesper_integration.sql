-- Vesper integration (see docs/vesper.md). Idempotent: the recommendation API applies every
-- migration on start-up (services/recommendation-api/app/migrate.py), so existing installs
-- pick this up without a manual psql step.

-- Mood mode: a temporary overlay ("Tonight I want something light") that re-ranks picks until
-- it expires. Never touches the behaviour-derived taste vector.
ALTER TABLE user_overlay ADD COLUMN IF NOT EXISTS mood TEXT;
ALTER TABLE user_overlay ADD COLUMN IF NOT EXISTS mood_expires_at TIMESTAMPTZ;

-- Cross-id joins: the curator and Soon Gone speak tmdb/tvdb ids (Radarr/Sonarr), the Jellyfin
-- adapter and Vesper speak Jellyfin item ids. Events carry metadata.tmdb_id / tvdb_id so the
-- two can be matched (protection set, Soon Gone whitelisting, Vesper's Keep button).
CREATE INDEX IF NOT EXISTS idx_event_tmdb ON event ((metadata->>'tmdb_id')) WHERE metadata ? 'tmdb_id';
CREATE INDEX IF NOT EXISTS idx_event_tvdb ON event ((metadata->>'tvdb_id')) WHERE metadata ? 'tvdb_id';

-- Per-user, per-action scans (wrapped, prefetch, history anchors).
CREATE INDEX IF NOT EXISTS idx_event_user_action_time ON event (user_id, action, "timestamp" DESC);

-- Linked identities: one person, several apps. A Vesper profile and a Mono account each have
-- their own Muse user_id; linking makes one the canonical identity and the other an alias.
-- The ingestion API writes alias events onto the canonical user (metadata.original_user_id
-- keeps the origin, so an unlink can move them back), and every read resolves aliases first.
CREATE TABLE IF NOT EXISTS identity_link (
    alias_user_id   UUID PRIMARY KEY,
    user_id         UUID NOT NULL,              -- canonical
    source          TEXT,                        -- 'vesper' | 'mono' | 'jellyfin' | ...
    label           TEXT,                        -- shown in the linking UI ("Mono · maxime")
    linked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (alias_user_id <> user_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_link_user ON identity_link (user_id);

-- Short-lived pairing codes ("Enter this code in Mono: 4KX-9TQ").
CREATE TABLE IF NOT EXISTS identity_link_code (
    code            TEXT PRIMARY KEY,
    user_id         UUID NOT NULL,
    source          TEXT,
    label           TEXT,
    expires_at      TIMESTAMPTZ NOT NULL
);

-- Who watches together: a sorted set of canonical user_ids seen co-viewing, so group
-- recommendations can lean on what the group actually watched together.
CREATE TABLE IF NOT EXISTS watch_group (
    members         UUID[] PRIMARY KEY,          -- sorted
    sessions        INTEGER NOT NULL DEFAULT 0,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
