-- Curator v2 and admin notifications.
--
-- * curator_managed_item learns real sizes (Radarr/Sonarr's sizeOnDisk), which seasons it
--   monitors, whether it was adopted (tagged in Radarr/Sonarr by a person) and whether the admin
--   pinned it ("keep forever").
-- * curator_setting: a few household-level switches the admin can flip from Vesper/Mono
--   (pause, budget) without editing .env.
-- * curator_activity: what the curator did and why, for the dashboard. The details (title,
--   reason) are sealed with the master key like everything else descriptive.
-- * admin_notification: the admin's feed, shown in Vesper (video) and Mono (music).
-- Safe to re-run.

ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS size_bytes BIGINT;            -- real, from the arr API; NULL until synced
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS size_synced_at TIMESTAMPTZ;
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS seasons_monitored INTEGER;     -- series: highest monitored season
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS seasons_total INTEGER;
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS adopted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS pinned_by TEXT;
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS data JSONB;                   -- sealed: title, year, reason

CREATE TABLE IF NOT EXISTS curator_setting (
    id              BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),   -- singleton row
    paused          BOOLEAN NOT NULL DEFAULT FALSE,
    budget_gb       INTEGER,                                       -- NULL: MUSE_CURATOR_BUDGET_GB
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_cycle_at   TIMESTAMPTZ,
    last_status     JSONB                                          -- disk space, usage, errors of the last cycle (no titles)
);
INSERT INTO curator_setting (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING;
ALTER TABLE curator_setting ADD COLUMN IF NOT EXISTS run_requested_at TIMESTAMPTZ;   -- "run now" from the admin

-- The admin chose "remove now": skip the protection re-check at expiry.
ALTER TABLE soon_gone ADD COLUMN IF NOT EXISTS forced BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS curator_activity (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL,     -- added | soon_gone | kept | removed | adopted | season_added | skipped | saved
    item_type       TEXT,
    source_item_id  TEXT,              -- TMDB id (as curator_managed_item)
    data            JSONB              -- sealed: title, year, score, reason
);
CREATE INDEX IF NOT EXISTS idx_curator_activity_at ON curator_activity (at DESC);

CREATE TABLE IF NOT EXISTS admin_notification (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    domain          TEXT NOT NULL CHECK (domain IN ('video', 'music', 'system')),
    kind            TEXT NOT NULL,     -- curator.added | curator.soon_gone | curator.removed | curator.disk_low | music.wanted | music.new_release | ...
    severity        TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'action', 'warning')),
    item_type       TEXT,
    source_item_id  TEXT,
    data            JSONB NOT NULL,    -- sealed: title, body, actions
    dedupe_key      TEXT UNIQUE,
    read_at         TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ        -- an action was taken (kept, removed) or it no longer applies
);
CREATE INDEX IF NOT EXISTS idx_admin_notification_feed ON admin_notification (domain, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_notification_unread ON admin_notification (created_at DESC) WHERE read_at IS NULL;

-- One-time pairing codes for the setup QR: a phone scans it and receives the client
-- configuration (URLs, keys, CA) once. Only a hash of the code is stored.
CREATE TABLE IF NOT EXISTS pairing_code (
    code_hash       TEXT PRIMARY KEY,
    purpose         TEXT NOT NULL CHECK (purpose IN ('vesper', 'mono', 'admin')),
    payload         JSONB NOT NULL,    -- sealed: what the scanning client receives
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ
);
