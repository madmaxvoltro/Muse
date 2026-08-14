# Muse Jellyfin plugin

Status: not yet implemented.

C#/.NET Jellyfin plugin, deliberately scoped to the official `IHomeScreenSection` plugin API
(Jellyfin 10.9+) rather than forking `jellyfin-web` — no client fork to maintain across
Jellyfin updates.

Responsibilities:
- Adds homepage rows: "Recommended for You", "Continue based on taste", "Soon Gone" — alongside
  Jellyfin's native rows, never replacing them.
- Calls the Muse Recommendation API (`services/recommendation-api`) for row contents.
- Hooks Jellyfin's playback-start event to call the Recommendation API's whitelist endpoint when
  a user opens a "Soon Gone" item (see `docs/architecture.md` — Soon Gone mechanism).

See `../docs/adapters/jellyfin.md` for setup once implemented.
