# Muse

A self-hosted, privacy-first unified taste-profile and recommendation system. Muse combines
signal from Jellyfin, the arr-stack (Sonarr/Radarr/Prowlarr), a FreeTube-like YouTube frontend,
SearxNG, Open WebUI, Navidrome, and Audiobookshelf into one cross-domain taste profile, and serves
active recommendations — primarily as native rows inside Jellyfin.

Existing recommendation systems don't talk to each other: YouTube's algorithm, Jellyfin's
"Continue Watching", your search history — all separate. Muse unifies them into one profile that
actually drives what gets suggested and, optionally, what gets downloaded.

## What it does

- **Unified event store**: every watch/search/listen/skip across all connected sources lands in
  one append-only Postgres+TimescaleDB table, negative signals (skipped/rejected) included from
  day one.
- **Cross-domain taste profile**: local-model embeddings, no external API calls — content never
  leaves your network for profiling purposes.
- **Google-level recommendation funnel**: candidate generation → ranking → re-ranking, with
  confidence as a first-class scoring input (not just a UI label) and a lightweight
  multi-armed-bandit explore mechanism.
- **User control overlay**: sliders, pins, an explore-dial, and per-recommendation explainability
  — sits on top of the automatic profile, never mutates it, always reversible.
- **Autonomous discovery/curator**: optionally lets Muse manage a fixed storage budget
  (add *and* remove content) against Radarr/Sonarr, with a "Soon Gone" 48h grace window and
  whitelist-by-watching so nothing you actually want gets silently deleted.
- **Multi-user**: auto-imports Jellyfin users as profiles; one person's accounts on different
  services (Jellyfin, Mono, ...) are linked explicitly with single-use codes into one profile.
- **Understands taste, not just tags**: listens to the music itself (tempo, key, energy, and an
  audio model for how it *sounds*), learns from what is played together, finds the distinct sides
  of someone's taste, and learns what they pick within a style.
- **Music beyond the library**: a catalog from MusicBrainz / ListenBrainz / Deezer, new releases
  of loved artists, and a wanted list for a download tool (API, webhooks, Lidarr-style list).
- **Flow**: next-track and radio that fit the person *and* the previous song (harmonic mixing,
  tempo, energy arcs).
- **The moment and the room**: moods (presets or free text), activity, device and time of day,
  weather, calendars, Home Assistant presence; group picks for any number of concurrent groups,
  fair to everyone.
- **Teachable, and assistant-ready**: a query API (search, profile, recommendations incl.
  "use my music taste for films") and an MCP server let an AI assistant know your taste, ask the
  questions Muse has about you, and tell Muse what it learns — which then shapes recommendations.

Full design rationale: [`docs/architecture.md`](docs/architecture.md).

## Status

Core pipeline implemented and unit-tested: event schema, ingestion API, embedding worker,
recommendation API (full 3-stage funnel), curator (300GB budget + Soon Gone), and the
SearxNG/Jellyfin/arr adapters. A local `docker compose build` succeeded for the Python
services; a full `up` wasn't completed in the dev sandbox (Docker Hub rate-limited image
pulls there) — verify on your actual host. The Jellyfin plugin (C#/.NET) is written but not
yet compiled/tested against a real Jellyfin instance. Still not started: Open WebUI,
Navidrome, and Audiobookshelf adapters.

Added since (see `docs/api/README.md` for everything as API): the catalog, audio analyzer,
deep taste (facets, sound, behaviour, preference models), the new engine with next/radio/groups,
context and moods, goals, imports, exports and privacy controls, webhooks and a weekly digest,
Navidrome and Audiobookshelf adapters, a learned ranker. A data-security audit is in
`docs/security-audit.md`. Earlier: Mono as a music source (see `docs/adapters/mono.md`), linked identities, facts and
questions that feed the taste vector, the query API (`services/query-api`) and the MCP server
(`services/mcp-server`). These are covered by an end-to-end test suite (`tests/`, runs the
embedding worker, recommendation API, query API and MCP server against a real Postgres). See `docs/architecture.md` for the full design, and
the `README.md` in each `services/*`/`adapters/*`/`jellyfin-plugin/` directory for
per-component status and known gaps.

## Repo layout

```
shared/muse_schema/     the Event model — the one contract every adapter and service agrees on
db/migrations/           Postgres/TimescaleDB schema
services/ingestion-api/  write-only endpoint all adapters POST events to
services/recommendation-api/  the 3-stage recommendation funnel
services/embedding-worker/    content + taste embedding computation
services/query-api/           the API for apps and assistants (search, profile, teach, context, groups, data)
services/mcp-server/          MCP server for AI assistants (thin client of the query API)
services/catalog/             music beyond the library: public databases, new releases, wanted list
services/audio-analyzer/      listens to music: features + CLAP embeddings (CPU or GPU)
services/scheduler/           weather, calendars, webhooks, digest, goals, retention
services/curator/             autonomous storage-budget manager
adapters/                one subdirectory per source, each a small standalone service
jellyfin-plugin/          C#/.NET plugin, IHomeScreenSection-scoped
docs/                     architecture.md, installation.md, api/ (API guide + OpenAPI), security-audit.md, adapters/
tests/                    end-to-end tests (MUSE_TEST_DATABASE_URL=... pytest)
```

## Getting started

See [`docs/installation.md`](docs/installation.md) for network-isolation prerequisites and setup.

## Security

Muse's event store aggregates a behavioral profile across nearly all of your personal data — treat
it as your highest-value target, not a toy. Deployment assumes a dedicated, network-isolated host
(own VLAN, no direct external exposure, Tailscale-only access) and encryption at rest. See the
"Security model" section of [`docs/architecture.md`](docs/architecture.md) before deploying.

## License

[MIT](LICENSE)
