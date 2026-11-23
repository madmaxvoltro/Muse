-- The multi-space vector worker (services/embedding-worker/vector_worker.py: content and taste
-- vectors, deep taste, the learned ranker) and the taste-engine worker (worker.py) both process
-- the event log. Each keeps its own watermark, so neither skips what the other already saw.
CREATE TABLE IF NOT EXISTS vector_worker_state (
    id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),  -- singleton row
    last_processed_at   TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z'
);

INSERT INTO vector_worker_state (id, last_processed_at)
VALUES (TRUE, '1970-01-01T00:00:00Z')
ON CONFLICT (id) DO NOTHING;

-- Impressions of the multi-space engine keep the item's stored (blind) id next to the real one,
-- so the ranker can join them to events in SQL (event.source_item_id is a blind index when
-- encryption is on, see muse_schema.storage).
ALTER TABLE engine_impression ADD COLUMN IF NOT EXISTS item_bid TEXT;
