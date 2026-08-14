# Curator

Status: not yet implemented.

Autonomous storage-budget manager. Daily cycle:
1. Score external catalog candidates (TMDB + Trakt) against combined taste vectors.
2. Score currently-owned auto-quota items for removal (low score + old + protected-set exclusions).
3. Swap within the 300GB budget, respecting the per-user dynamic daily download cap (up to 10/day,
   scaled down by recent activity) — never touches files directly, only calls Radarr/Sonarr's API
   to add/remove.
4. Removal candidates go through the "Soon Gone" 48h grace window (see `db/migrations/001_init_event_store.sql`,
   table `soon_gone`) before an actual remove command is sent.

See `../../docs/architecture.md` for the full design.
