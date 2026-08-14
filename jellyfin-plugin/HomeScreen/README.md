# Home screen sections — verification needed

`IHomeScreenSection` (Jellyfin 10.9+) is a newer, less-documented part of the plugin ABI
than `IServerEntryPoint`/`BasePlugin` — its exact interface shape (property names, the
`GetResults`/`GetInfo` signatures, what `HomeScreenSectionPayload` exposes) varies more
between Jellyfin point releases. The three sections here (`RecommendedForYouSection.cs`,
`ContinueBasedOnTasteSection.cs`, `SoonGoneSection.cs`) are written to the best available
understanding of that interface, but **have not been compiled against a real
`Jellyfin.Controller` package** (no .NET SDK / Jellyfin dev environment was available while
writing this — see `jellyfin-plugin/README.md`).

Before relying on these:
1. `dotnet add package Jellyfin.Controller` at the version matching your actual Jellyfin
   server, and fix whatever the compiler flags — the shared logic (calling `MuseApiClient`,
   mapping `source_item_id` back to a Jellyfin item) should need no changes even if the
   interface signatures do.
2. Confirm the plugin's registered sections actually appear as homepage row options in
   Jellyfin's UI (Dashboard → users can usually toggle which sections show).

The `PlaybackStartEntryPoint` in `../EntryPoints/` is on much more stable ground
(`ISessionManager` has been part of the plugin ABI for years) and needs no such caveat.
