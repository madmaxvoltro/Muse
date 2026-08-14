# Muse adapter: jellyfin

Status: implemented (poll-based; see `docs/adapters/jellyfin.md` for setup and the note on
the future C#/.NET Jellyfin plugin taking over live playback events).

Two jobs, own intervals:
- **Catalog sync** (default: daily) — walks the whole library, emits a zero-weight `cataloged`
  event per item so the embedding worker can build content embeddings for a library that
  already exists, without waiting for new watch history.
- **Watch-state sync** (default: every 5 min) — diffs `UserData` (Played/PlaybackPositionTicks/
  IsFavorite) per item per user, emits `played`/`completed`/`favorited` events on change.

Uses Jellyfin's own per-user GUID directly as the Muse `user_id` — see
`docs/architecture.md` (Multi-user section).
