-- Radarr/Sonarr assign their own internal ID on add, distinct from the tmdb/tvdb ID
-- Muse scores candidates by. Needed to call the delete endpoint later (Soon Gone expiry).
ALTER TABLE curator_managed_item ADD COLUMN IF NOT EXISTS arr_id TEXT;
ALTER TABLE soon_gone ADD COLUMN IF NOT EXISTS arr_id TEXT;
