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
- **Multi-user**: auto-imports Jellyfin users as profiles, with privacy-safe identity resolution
  across your other self-hosted accounts.

Full design rationale: [`docs/architecture.md`](docs/architecture.md).

## Status

Early scaffolding — event schema, ingestion API, and the SearxNG adapter (reference
implementation) are functional. Everything else (embedding worker, recommendation funnel,
curator, Jellyfin plugin, remaining adapters) is stubbed out with the design decided but not yet
implemented. See `docs/architecture.md` for what's designed, and the `README.md` in each
`services/*` and `adapters/*` directory for per-component status.

## Repo layout

```
shared/muse_schema/     the Event model — the one contract every adapter and service agrees on
db/migrations/           Postgres/TimescaleDB schema
services/ingestion-api/  write-only endpoint all adapters POST events to
services/recommendation-api/  the 3-stage recommendation funnel
services/embedding-worker/    content + taste embedding computation
services/curator/             autonomous storage-budget manager
adapters/                one subdirectory per source, each a small standalone service
jellyfin-plugin/          C#/.NET plugin, IHomeScreenSection-scoped
docs/                     architecture.md + installation.md + one setup guide per adapter
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
