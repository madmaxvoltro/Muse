# Muse adapter: arr

Status: implemented (poll-based, same pattern as the Jellyfin adapter).

Polls Radarr + Sonarr, diffs library state against the last poll:
- New item appears -> `added_watchlist`
- Item disappears, never had a file -> `rejected` (added but never fulfilled, discarded —
  a clear "bad add" signal)
- Item disappears, had a file -> `removed` (milder — was consumed to some degree)

First run backfills the entire current Radarr/Sonarr library as `added_watchlist`, same
backfill idea as the Jellyfin adapter's catalog sync.

No per-user attribution (Radarr/Sonarr don't track who added what) — uses
`MUSE_DEFAULT_USER_ID`, same v1 fallback as the SearxNG adapter.

See `docs/adapters/arr.md` for setup, and `docs/architecture.md` for why this adapter
doesn't need to know whether a removal was curator-driven or manual.
