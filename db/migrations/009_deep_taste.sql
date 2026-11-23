-- Deep taste: facets (several tastes per person), per-person preference models over sound,
-- and item-to-item transitions learnt from listening sessions. Vectors live in Qdrant
-- (taste_facets, taste_facets_audio, audio_taste, behavior_items, behavior_taste).
-- Safe to re-run:  docker compose exec -T postgres psql -U muse -d muse < db/migrations/009_deep_taste.sql

CREATE TABLE IF NOT EXISTS taste_facet (
    user_id         UUID NOT NULL,
    item_type       TEXT NOT NULL,
    space           TEXT NOT NULL CHECK (space IN ('text', 'audio')),
    idx             INTEGER NOT NULL,
    share           REAL NOT NULL,                  -- share of the person's positive weight
    size            INTEGER NOT NULL,
    label           TEXT NOT NULL,                  -- "dreamy, female vocals, synthesizers" / "dutch rock (Kensington, Racoon)"
    descriptors     JSONB NOT NULL DEFAULT '[]'::jsonb,
    examples        JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{source, source_item_id, title}]
    features        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- average bpm/energy/... of its audio
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, item_type, space, idx)
);

CREATE TABLE IF NOT EXISTS taste_model (
    user_id         UUID NOT NULL,
    item_type       TEXT NOT NULL,
    kind            TEXT NOT NULL,                  -- 'audio_pref'
    model           JSONB NOT NULL,
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    trained_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, item_type, kind)
);

-- A -> B: how often B followed A in someone's session in this household, and how it went.
CREATE TABLE IF NOT EXISTS item_transition (
    from_key        TEXT NOT NULL,                  -- "source:source_item_id"
    to_key          TEXT NOT NULL,
    count           INTEGER NOT NULL DEFAULT 0,
    completions     INTEGER NOT NULL DEFAULT 0,
    skips           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (from_key, to_key)
);
CREATE INDEX IF NOT EXISTS idx_item_transition_to ON item_transition (to_key);
