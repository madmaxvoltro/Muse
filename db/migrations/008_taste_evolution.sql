-- Taste evolution (docs/recommender.md, "Taste evolution"): one overlay slider.
-- 0 = evolution off (no trend/satiation/rediscovery/rhythm/stretch adjustments),
-- 1 = default (subtle), 2 = strong. The evolution report itself lives in taste_profile.profile.
-- Safe to re-run; apply by hand on existing databases:
--   docker compose exec -T postgres psql -U muse -d muse < db/migrations/008_taste_evolution.sql
ALTER TABLE user_overlay ADD COLUMN IF NOT EXISTS evolution_strength REAL NOT NULL DEFAULT 1.0
    CHECK (evolution_strength >= 0 AND evolution_strength <= 2);
