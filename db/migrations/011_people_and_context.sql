-- People, linked identities, access tokens and what Muse is told rather than observes: facts,
-- goals, the current mood/context and feature ideas. Used by the query API (services/query-api).
-- Free text about a person is sealed (muse_schema.crypto, {"_enc": ...} like every sealed
-- column); only ids, times and kinds are plain.
-- Safe to re-run (applied automatically by the admin `migrate` step).

-- A person Muse knows about, created lazily (first token, first link, a display name).
CREATE TABLE IF NOT EXISTS muse_user (
    user_id         UUID PRIMARY KEY,
    display_name    JSONB,                  -- sealed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Another id for the same person (Jellyfin GUID, Mono account, ...). Events keep the id they
-- were written with; readers resolve aliases (muse_schema.identity). Unlinking deletes the row.
CREATE TABLE IF NOT EXISTS user_alias (
    alias_id        UUID PRIMARY KEY,
    user_id         UUID NOT NULL,          -- canonical id
    source          TEXT,                   -- service the alias came from, if known
    label           JSONB,                  -- sealed, e.g. "Mono"
    linked_by       TEXT NOT NULL,          -- 'link_code' | 'admin'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (alias_id <> user_id)
);
CREATE INDEX IF NOT EXISTS idx_user_alias_user ON user_alias (user_id);

-- Single-use codes: create one as identity A, redeem it as identity B, and B joins A.
CREATE TABLE IF NOT EXISTS link_code (
    code_hash       TEXT PRIMARY KEY,       -- keyed digest of the normalised code
    user_id         UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    used_by         UUID
);

-- Bearer tokens for the query API / MCP. user_id NULL = household admin token.
CREATE TABLE IF NOT EXISTS api_token (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash      TEXT NOT NULL UNIQUE,   -- keyed digest; the token is shown once
    user_id         UUID,
    label           JSONB,                  -- sealed
    can_write       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);

-- Things Muse was told: "loves slow Dutch songs", "no horror". `likes` facts become interests
-- in the taste profile of their domain (embedded like pinned topics); the rest are kept, shown
-- and used by assistants.
CREATE TABLE IF NOT EXISTS profile_fact (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('likes', 'dislikes', 'avoid', 'context', 'fact')),
    domain          TEXT NOT NULL DEFAULT 'general',   -- video | youtube | music | books | general
    statement       JSONB NOT NULL,                    -- sealed
    confidence      REAL NOT NULL DEFAULT 0.7 CHECK (confidence BETWEEN 0 AND 1),
    source          TEXT NOT NULL DEFAULT 'assistant', -- 'assistant' | 'user' | adapter name
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_profile_fact_user ON profile_fact (user_id) WHERE retracted_at IS NULL;

-- Goals ("discover 20 new artists this year"). Progress is computed, the text is sealed.
CREATE TABLE IF NOT EXISTS user_goal (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    data            JSONB NOT NULL,         -- sealed {"title", "kind", "target", "domain", "until"}
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at         TIMESTAMPTZ,
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_user_goal_user ON user_goal (user_id) WHERE deleted_at IS NULL;

-- The current situation: a mood with an expiry and context signals (device, weather,
-- calendar, Home Assistant). The recommendation API uses the mood when a request names none.
CREATE TABLE IF NOT EXISTS user_context (
    user_id         UUID PRIMARY KEY,
    mood            JSONB,                  -- sealed
    mood_until      TIMESTAMPTZ,
    signals         JSONB,                  -- sealed {"device": ..., "weather": ..., ...}
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ideas for Muse itself, mostly from assistants that notice something missing.
CREATE TABLE IF NOT EXISTS feature_suggestion (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,
    data            JSONB NOT NULL,         -- sealed {"title", "description", "rationale"}
    created_by      TEXT NOT NULL DEFAULT 'assistant',
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'planned', 'done', 'declined')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Users whose profiles must be rebuilt although no event arrived (a fact, a link).
CREATE TABLE IF NOT EXISTS taste_dirty (
    user_id         UUID PRIMARY KEY,
    marked_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-person settings, e.g. {"incognito_until": "2026-01-01T12:00:00+00:00"}.
ALTER TABLE user_overlay ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'::jsonb;
