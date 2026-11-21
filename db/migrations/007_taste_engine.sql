-- Taste engine v2 (see docs/recommender.md): item catalog with canonical identity and
-- enrichment, stored multi-interest profiles, impression log, learned ranker weights.
-- Safe to re-run. On databases created before this file existed apply by hand:
--   docker compose exec -T postgres psql -U muse -d muse < db/migrations/007_taste_engine.sql

-- One row per (source, source_item_id) the event log has mentioned, plus TMDB items Muse
-- discovered through enrichment (source = 'tmdb'). canon_key ties rows that are the same
-- real-world title together (muse_taste.canon); taste and exclusion work on canon_key.
CREATE TABLE IF NOT EXISTS item (
    item_key        TEXT PRIMARY KEY,               -- '<source>:<source_item_id>'
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    item_type       TEXT NOT NULL,
    canon_key       TEXT NOT NULL,
    parent_key      TEXT,                            -- episodes: canon key of the series
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- latest non-empty adapter metadata
    enrichment      JSONB,                           -- TMDB (muse_taste.tmdb.parse_details)
    llm_profile     JSONB,                           -- optional LLM tone/themes/audience
    available       BOOLEAN NOT NULL DEFAULT FALSE,  -- playable in the library (Jellyfin)
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    enriched_at     TIMESTAMPTZ,
    enrich_failures INTEGER NOT NULL DEFAULT 0,
    llm_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_item_canon ON item (canon_key);
CREATE INDEX IF NOT EXISTS idx_item_enrich_todo ON item (enriched_at NULLS FIRST) WHERE source <> 'tmdb';
CREATE INDEX IF NOT EXISTS idx_item_parent ON item (parent_key) WHERE parent_key IS NOT NULL;

-- One row per canonical title: what gets embedded. doc_hash changes when the document text
-- or the embedding model changes, which is how the worker knows what to (re-)embed.
CREATE TABLE IF NOT EXISTS canon_item (
    canon_key       TEXT PRIMARY KEY,
    item_type       TEXT NOT NULL,
    domain          TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    year            INTEGER,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    related         TEXT[] NOT NULL DEFAULT '{}',    -- canon keys of TMDB recommendations, ranked
    collection_id   TEXT,
    vote_average    REAL,
    vote_count      REAL,
    popularity      REAL,
    available       BOOLEAN NOT NULL DEFAULT FALSE,
    library_refs    JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{"source":"jellyfin","source_item_id":"..."}]
    added_at        TIMESTAMPTZ,                     -- first time it appeared in the library
    document        TEXT NOT NULL DEFAULT '',        -- what the embedder sees (muse_taste.documents)
    doc_hash        TEXT,                            -- hash(model, document): re-embed when it changes
    embedded_hash   TEXT,
    payload_hash    TEXT,                            -- hash of the Qdrant payload fields
    synced_payload_hash TEXT,                        -- payload-only changes skip re-embedding
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_canon_embed_todo ON canon_item (canon_key) WHERE embedded_hash IS DISTINCT FROM doc_hash;
CREATE INDEX IF NOT EXISTS idx_canon_domain ON canon_item (domain);

-- Stored taste profiles (muse_taste.profile.TasteProfile.to_json), one per user and domain.
CREATE TABLE IF NOT EXISTS taste_profile (
    user_id     UUID NOT NULL,
    domain      TEXT NOT NULL,
    profile     JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, domain)
);

-- Every recommendation shown. Feeds fatigue (shown often, never picked), the exploration
-- loop and ranker weight learning. features = the ranker's feature vector at serve time.
CREATE TABLE IF NOT EXISTS impression (
    id          BIGSERIAL PRIMARY KEY,
    request_id  UUID NOT NULL,
    user_id     UUID NOT NULL,
    canon_key   TEXT NOT NULL,
    surface     TEXT NOT NULL DEFAULT 'home',
    position    INTEGER NOT NULL,
    score       REAL NOT NULL,
    sigma       REAL NOT NULL,
    explore     BOOLEAN NOT NULL DEFAULT FALSE,
    features    JSONB NOT NULL DEFAULT '{}'::jsonb,
    shown_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_impression_user_time ON impression (user_id, shown_at DESC);
CREATE INDEX IF NOT EXISTS idx_impression_user_item ON impression (user_id, canon_key);

-- Ranker weights fitted from impressions (muse_taste.learn). Latest row wins.
CREATE TABLE IF NOT EXISTS ranker_model (
    id          BIGSERIAL PRIMARY KEY,
    weights     JSONB NOT NULL,
    adopted     BOOLEAN NOT NULL,
    auc_default REAL,
    auc_learned REAL,
    n_train     INTEGER,
    n_valid     INTEGER,
    note        TEXT,
    fitted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Questions Muse asked, so it doesn't ask the same thing twice.
CREATE TABLE IF NOT EXISTS question_log (
    id          UUID PRIMARY KEY,
    user_id     UUID NOT NULL,
    kind        TEXT NOT NULL,          -- onboarding | this_or_that | abandon
    payload     JSONB NOT NULL,
    asked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_question_user ON question_log (user_id, asked_at DESC);

-- Embedding worker: remember which model the vectors were made with (a change means re-embed).
ALTER TABLE embedding_worker_state ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE embedding_worker_state ADD COLUMN IF NOT EXISTS vector_dim INTEGER;
ALTER TABLE embedding_worker_state ADD COLUMN IF NOT EXISTS last_full_profile_at TIMESTAMPTZ;
ALTER TABLE embedding_worker_state ADD COLUMN IF NOT EXISTS last_ranker_fit_at TIMESTAMPTZ;

-- Per-user lookups by canonical key (exclusion, responsiveness) go through item.
CREATE INDEX IF NOT EXISTS idx_event_user_ingested ON event (user_id, ingested_at DESC);
