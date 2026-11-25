-- Operations: migration bookkeeping and the worker heartbeat shown by `muse status` and the
-- status page. Safe to re-run (applied automatically by the admin `migrate` step).
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE embedding_worker_state ADD COLUMN IF NOT EXISTS last_cycle_at TIMESTAMPTZ;
ALTER TABLE embedding_worker_state ADD COLUMN IF NOT EXISTS last_cycle_report JSONB;
ALTER TABLE embedding_worker_state ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
