-- When Muse received an event, separate from when it happened ("timestamp"). The embedding
-- worker tracks its progress by this, so history imported late (old timestamps) still gets
-- embedded. Safe to re-run; apply by hand on databases created before this file existed:
ALTER TABLE event ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS event_ingested_at_idx ON event (ingested_at);
