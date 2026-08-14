-- Tracks the embedding worker's progress through the event log so each run
-- only processes events written since the last run (no reprocessing the
-- whole table every cycle).
CREATE TABLE IF NOT EXISTS embedding_worker_state (
    id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- singleton row
    last_processed_at   TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z'
);

INSERT INTO embedding_worker_state (id, last_processed_at)
VALUES (TRUE, '1970-01-01T00:00:00Z')
ON CONFLICT (id) DO NOTHING;
