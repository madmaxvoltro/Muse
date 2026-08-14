-- Items the curator has added and is tracking against the 300GB auto-quota.
-- Distinct from the event store's `event` table: this is inventory state, not a log.
CREATE TABLE IF NOT EXISTS curator_managed_item (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT NOT NULL,          -- 'arr' (Radarr/Sonarr)
    source_item_id  TEXT NOT NULL,
    item_type       TEXT NOT NULL,
    estimated_bytes BIGINT NOT NULL,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at      TIMESTAMPTZ,
    UNIQUE (source, source_item_id)
);

CREATE INDEX IF NOT EXISTS idx_curator_managed_active ON curator_managed_item (source_item_id) WHERE removed_at IS NULL;

-- One row per curator-triggered download, used to enforce the per-user daily cap
-- (10/day ceiling, scaled down for low-activity users — see docs/architecture.md).
CREATE TABLE IF NOT EXISTS curator_download_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL,
    source_item_id TEXT NOT NULL,
    downloaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_curator_download_log_user_time ON curator_download_log (user_id, downloaded_at DESC);
