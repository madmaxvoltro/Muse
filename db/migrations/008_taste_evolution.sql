-- Taste evolution (docs/recommender.md, "Taste evolution"): one overlay slider.
-- 0 = evolution off (no trend/satiation/rediscovery/rhythm/stretch adjustments),
-- 1 = default (subtle), 2 = strong. The evolution report itself lives in taste_profile.profile.
-- Safe to re-run; applied automatically by the admin migrate step (./muse up).
ALTER TABLE user_overlay ADD COLUMN IF NOT EXISTS evolution_strength REAL NOT NULL DEFAULT 1.0
    CHECK (evolution_strength >= 0 AND evolution_strength <= 2);
