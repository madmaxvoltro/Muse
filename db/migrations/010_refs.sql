-- People as other systems know them (Home Assistant person entities, ...), so presence and
-- other signals can name Muse users. Safe to re-run.
CREATE TABLE IF NOT EXISTS person_ref (
    system          TEXT NOT NULL,                  -- 'home_assistant', ...
    ref             TEXT NOT NULL,                  -- e.g. 'person.max'
    user_id         UUID NOT NULL,                  -- canonical
    PRIMARY KEY (system, ref)
);
-- Which rooms currently have which people (fed by Home Assistant presence); drives automatic
-- group sessions per room.
CREATE TABLE IF NOT EXISTS room_presence (
    room            TEXT NOT NULL,
    user_id         UUID NOT NULL,
    since           TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    PRIMARY KEY (room, user_id)
);
