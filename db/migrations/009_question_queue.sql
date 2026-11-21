-- Question queue (docs/recommender.md, "Muse asks questions"). question_log becomes a queue
-- the worker fills in the background; clients fetch from it through the API or MCP server
-- if they want to. Nothing depends on answers: unanswered questions simply expire.
-- Safe to re-run; apply by hand on existing databases:
--   docker compose exec -T postgres psql -U muse -d muse < db/migrations/009_question_queue.sql

-- Rows that existed before this migration were asked on demand: mark them served.
ALTER TABLE question_log ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'served';
ALTER TABLE question_log ALTER COLUMN status SET DEFAULT 'pending';  -- pending | served | answered | dismissed | expired
ALTER TABLE question_log ADD COLUMN IF NOT EXISTS domain TEXT;
ALTER TABLE question_log ADD COLUMN IF NOT EXISTS priority REAL NOT NULL DEFAULT 0.5;
ALTER TABLE question_log ADD COLUMN IF NOT EXISTS dedupe_key TEXT;
ALTER TABLE question_log ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE question_log ADD COLUMN IF NOT EXISTS served_at TIMESTAMPTZ;
ALTER TABLE question_log ADD COLUMN IF NOT EXISTS answer JSONB;

CREATE INDEX IF NOT EXISTS idx_question_queue ON question_log (user_id, priority DESC) WHERE status IN ('pending', 'served');
CREATE INDEX IF NOT EXISTS idx_question_dedupe ON question_log (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL;
