# Curator

Status: implemented, including real Radarr/Sonarr add/remove payloads in `arr_client.py`
(lookup-by-tmdb/tvdb-id -> merge quality profile + root folder -> add; delete-by-arr-id ->
remove). Untested against a live Radarr/Sonarr instance — see Known v1 gaps.

Autonomous storage-budget manager. See `docs/architecture.md` (Discovery/acquisition
pipeline) for the full design; this directory implements it:

- `discovery.py` — TMDB + Trakt candidate fetching (outbound-only, separate trust boundary
  from the inbound adapters)
- `scoring.py` — averaged-across-profiles similarity for ADD decisions, max-across-profiles
  for REMOVE decisions (an item is only removal-eligible if *every* active profile scores it low)
- `budget.py` — the 300GB ceiling, the dynamic per-user daily cap (up to 10/day, scaled down
  by 14-day activity), the always-protected set (favorited + partially-watched), and the
  "Soon Gone" 48h grace mechanism
- `arr_client.py` — the only code path that can actually cause a file add/remove; everything
  else only ever calls through here
- `main.py` — two loops: frequent Soon-Gone expiry checks, and a slow daily add/remove cycle

## Known v1 gaps

- `arr_client.py` is implemented but untested against a live Radarr/Sonarr instance — the
  exact response shape from the lookup/add endpoints should be verified against a real
  install before relying on this in production. See `docs/adapters/arr.md`.
- Item byte sizes are estimated (`discovery.ESTIMATED_BYTES`), not queried from the arr-stack
  post-download — good enough for budget math, not exact.
- The Jellyfin plugin's playback-start whitelist hook (which pulls a "Soon Gone" item out of
  the grace window when someone watches it) isn't built yet — this curator can mark items
  soon-gone and expire them, but nothing can whitelist one yet. See `jellyfin-plugin/README.md`.
