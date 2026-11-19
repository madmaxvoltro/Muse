-- Idempotent ingestion (metadata.dedupe_key, see services/ingestion-api): look-ups by key.
CREATE INDEX IF NOT EXISTS idx_event_dedupe ON event (user_id, source, (metadata->>'dedupe_key'))
    WHERE metadata ? 'dedupe_key';
