# Muse Jellyfin plugin

Status: written, **not yet compiled/tested** — no .NET SDK or Jellyfin dev environment was
available while writing this (see verification steps below). Structurally complete: plugin
bootstrap, configuration page, the playback-start whitelist hook, and three homepage-row
sections are all implemented against the Jellyfin plugin ABI as best understood.

C#/.NET plugin, deliberately scoped to the official `IHomeScreenSection` plugin API
(Jellyfin 10.9+) rather than forking `jellyfin-web` — see `docs/architecture.md` ("Jellyfin
plugin scope") for why. It only ever adds rows to the existing homepage, never restructures it.

## Layout

```
Plugin.cs                          plugin bootstrap (BasePlugin, IHasWebPages)
Configuration/
  PluginConfiguration.cs           stores the Recommendation API URL + row item limit
  configPage.html                  admin settings page
Services/
  MuseApiClient.cs                 thin HTTP client for the Recommendation API
EntryPoints/
  PlaybackStartEntryPoint.cs       ISessionManager.PlaybackStart hook -> Soon Gone whitelist
HomeScreen/
  MuseHomeScreenSectionBase.cs     shared logic: call API, resolve source_item_id -> BaseItem
  RecommendedForYouSection.cs      top of the 3-stage recommendation funnel
  ContinueBasedOnTasteSection.cs   NOT YET FUNCTIONAL — needs a new API endpoint, see its file
  SoonGoneSection.cs               items in their 48h grace window
  README.md                        why this half needs extra verification (see below)
```

## Two different confidence levels in this code

- **`EntryPoints/PlaybackStartEntryPoint.cs`** uses `ISessionManager`, part of the plugin
  ABI for years — solid ground, should need little to no changes.
- **`HomeScreen/*.cs`** uses `IHomeScreenSection` (Jellyfin 10.9+), a newer and
  less-documented part of the ABI whose exact interface shape varies more between point
  releases. Written to the best available understanding, but **must be verified against a
  real `Jellyfin.Controller` package** before relying on it — see `HomeScreen/README.md`.

## Building

Requires the .NET 8 SDK and a way to resolve `Jellyfin.Controller`/`Jellyfin.Model` — either
Jellyfin's own NuGet feed, or a local reference to a Jellyfin server checkout matching your
target version:

```bash
dotnet restore
dotnet build -c Release
```

Then copy the built `Muse.JellyfinPlugin.dll` into Jellyfin's plugin directory (see
`docs/installation.md` step 5), or host `build.yaml`/the built artifact as a plugin
repository and add that URL under Dashboard → Plugins → Repositories.

## What still needs a real Jellyfin instance to finish

1. Compile against the actual `Jellyfin.Controller` version you run, fix whatever the
   `IHomeScreenSection` interface's real signature disagrees with (see `HomeScreen/README.md`).
2. Confirm the config page renders correctly and saves (Dashboard → Plugins → Muse).
3. Confirm the three sections actually show up as selectable homepage rows.
4. Build the `GET /continue-watching/{user_id}` endpoint `ContinueBasedOnTasteSection.cs`
   needs on the Recommendation API side (not yet implemented — see that file's TODO).
5. End-to-end test the whitelist flow: mark something Soon Gone (curator), play it in
   Jellyfin, confirm `soon_gone.whitelisted` flips and the item stops counting toward the
   curator's removal candidates.
