-- People, linked identities, access tokens, and what Muse knows beyond the event log:
-- facts an assistant (or the person) told it, questions it wants answered, and feature ideas.
-- Safe to re-run; apply by hand on databases created before this file existed:
--   docker compose exec -T postgres psql -U muse -d muse < db/migrations/007_people_and_knowledge.sql

-- A person Muse knows about. Rows are created lazily (first token, first link, a display name).
CREATE TABLE IF NOT EXISTS muse_user (
    user_id         UUID PRIMARY KEY,
    display_name    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Another id for the same person (Jellyfin GUID, Mono account, ...). Events keep the id they
-- were written with; readers resolve aliases at read time (shared/muse_schema/identity.py).
-- Unlinking = deleting the row. A canonical id is never itself an alias.
CREATE TABLE IF NOT EXISTS user_alias (
    alias_id        UUID PRIMARY KEY,
    user_id         UUID NOT NULL,          -- canonical id
    source          TEXT,                   -- which service the alias id came from, if known
    label           TEXT,                   -- e.g. "Mono: max"
    linked_by       TEXT NOT NULL,          -- 'link_code' | 'admin'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (alias_id <> user_id)
);
CREATE INDEX IF NOT EXISTS idx_user_alias_user ON user_alias (user_id);

-- Short-lived single-use codes: create one as identity A, redeem it as identity B, and B joins A.
CREATE TABLE IF NOT EXISTS link_code (
    code_hash       TEXT PRIMARY KEY,       -- sha256 of the normalized code
    user_id         UUID NOT NULL,          -- canonical id of whoever created it
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    used_by         UUID
);

-- Bearer tokens for the query API / MCP. user_id NULL = household admin (all users).
CREATE TABLE IF NOT EXISTS api_token (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash      TEXT NOT NULL UNIQUE,   -- sha256, the token itself is shown once
    user_id         UUID,
    label           TEXT,
    can_write       BOOLEAN NOT NULL DEFAULT TRUE,  -- false: read-only (search, profile, recommendations)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);

-- Things Muse was told rather than observed. Facts with a sentiment are embedded by the
-- embedding worker and folded into the taste vector like a (weighted) item; 'avoid' facts with
-- a subject are also a hard filter in the recommendation API.
CREATE TABLE IF NOT EXISTS profile_fact (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,          -- canonical id
    kind            TEXT NOT NULL CHECK (kind IN ('likes', 'dislikes', 'avoid', 'context', 'fact')),
    domain          TEXT NOT NULL DEFAULT 'general',  -- an item_type, or 'general'
    statement       TEXT NOT NULL,          -- natural language, e.g. "Loves slow, sad Dutch songs"
    subject         TEXT,                   -- optional anchor: an artist, genre or title
    sentiment       REAL NOT NULL DEFAULT 0 CHECK (sentiment BETWEEN -1 AND 1),
    confidence      REAL NOT NULL DEFAULT 0.7 CHECK (confidence BETWEEN 0 AND 1),
    source          TEXT NOT NULL DEFAULT 'assistant',  -- 'assistant' | 'user' | 'question'
    origin_question UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_profile_fact_user ON profile_fact (user_id) WHERE retracted_at IS NULL;

-- Questions Muse (or an assistant) wants a person to answer. Options can carry an `effect`
-- that answering applies (a rating, a fact, an overlay setting) — see services/query-api.
CREATE TABLE IF NOT EXISTS question (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,          -- canonical id
    domain          TEXT NOT NULL DEFAULT 'general',
    kind            TEXT NOT NULL CHECK (kind IN ('choice', 'scale', 'yes_no', 'free_text', 'item_feedback')),
    prompt          TEXT NOT NULL,
    options         JSONB NOT NULL DEFAULT '[]'::jsonb,
    context         JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason          TEXT,                   -- why Muse asks: shown to the assistant, may be shown to the person
    priority        REAL NOT NULL DEFAULT 0.5,
    dedupe_key      TEXT,                   -- generator key, so an answered/skipped question isn't asked again
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'skipped', 'expired')),
    created_by      TEXT NOT NULL DEFAULT 'muse',  -- 'muse' | 'assistant' | adapter name
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    answered_at     TIMESTAMPTZ,
    answer          JSONB,
    answered_via    TEXT,
    UNIQUE (user_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_question_open ON question (user_id, priority DESC) WHERE status = 'open';

-- Ideas for Muse itself, mostly from assistants that notice something missing while helping.
CREATE TABLE IF NOT EXISTS feature_suggestion (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,                   -- who it came up with, if anyone in particular
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    rationale       TEXT,
    created_by      TEXT NOT NULL DEFAULT 'assistant',
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'planned', 'done', 'declined')),
    votes           INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Users whose taste vectors must be recomputed although no new event arrived (a fact was
-- added/retracted, identities were linked). The embedding worker drains this table.
CREATE TABLE IF NOT EXISTS taste_dirty (
    user_id         UUID PRIMARY KEY,
    marked_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Free-form per-person settings next to the existing sliders, e.g. {"taste_blend": 0.3}:
-- how much of the cross-domain (_combined) taste to mix into each domain's own.
ALTER TABLE user_overlay ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'::jsonb;
