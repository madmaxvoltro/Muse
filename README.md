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
- **Multi-interest taste profile**: one judgement per title (no more over-counting progress
  polls or binges), several interest clusters per person instead of one blurry average,
  disliked titles as repulsors, canonical identity across every source. Embeddings run locally
  (CPU or GPU) or on your own Ollama/vLLM; content understanding is enriched with TMDB
  keywords/credits and optional LLM tone descriptors.
- **Research-grounded recommendation funnel**: eight candidate generators fused with RRF
  (including TMDB "fans also liked" as borrowed collaborative filtering), an explainable
  ranker with per-candidate uncertainty, lists calibrated to your interest mix, and
  Thompson-sampled exploration. Weights re-fit from what you actually watch. Details and
  citations: [`docs/recommender.md`](docs/recommender.md).
- **Taste evolution**: tracks how each interest emerges, grows, fades and comes back, notices
  when you've had enough of something, learns your rhythms (weekday evenings vs Sunday
  mornings), and broadens your taste step by step beyond the edge of what you like. All subtle,
  explained, and adjustable per user; `GET /evolution/{user}` shows the timeline.
- **Muse asks the right questions, optionally**: a background-filled queue (onboarding picks,
  "this or that?" where it is least sure, "why did you stop?", and taste-evolution checks like
  "more of that?") served by the API and an optional MCP server. Nothing depends on answers.
  Plus history import from YouTube, Spotify, Letterboxd, IMDb, Netflix, Trakt and SoundCloud.
- **Measurable**: an offline evaluator replays your own history and reports Recall/nDCG/
  diversity/calibration for every stage, so changes are judged by numbers.
- **User control overlay**: sliders, pins, an explore-dial, and per-recommendation explainability
  — sits on top of the automatic profile, never mutates it, always reversible.
- **Autonomous discovery/curator**: optionally lets Muse manage a fixed storage budget
  (add *and* remove content) against Radarr/Sonarr, with a "Soon Gone" 48h grace window and
  whitelist-by-watching so nothing you actually want gets silently deleted.
- **Multi-user**: auto-imports Jellyfin users as profiles, with privacy-safe identity resolution
  across your other self-hosted accounts.

Full design rationale: [`docs/architecture.md`](docs/architecture.md).

## Status

Core pipeline implemented and tested (`pytest tests` — unit tests for the engine, integration
tests against Postgres + in-memory Qdrant): event schema, ingestion API, embedding worker,
taste engine (`shared/muse_taste`, see `docs/recommender.md`), recommendation API, curator
(300GB budget + Soon Gone), and the SearxNG/Jellyfin/arr adapters. A local `docker compose build` succeeded for the Python
services; a full `up` wasn't completed in the dev sandbox (Docker Hub rate-limited image
pulls there) — verify on your actual host. The Jellyfin plugin (C#/.NET) is written but not
yet compiled/tested against a real Jellyfin instance. Still not started: Open WebUI,
Navidrome, and Audiobookshelf adapters. See `docs/architecture.md` for the full design, and
the `README.md` in each `services/*`/`adapters/*`/`jellyfin-plugin/` directory for
per-component status and known gaps.

## Repo layout

```
shared/muse_schema/     the Event model — the one contract every adapter and service agrees on
shared/muse_taste/      the taste engine: signals, profiles, retrieval, ranking, reranking, questions, eval
db/migrations/           Postgres/TimescaleDB schema
services/ingestion-api/  write-only endpoint all adapters POST events to
services/recommendation-api/  the 3-stage recommendation funnel
services/embedding-worker/    content + taste embedding computation
services/curator/             autonomous storage-budget manager
services/mcp-server/          optional MCP server: questions, answers, recommendations, profile, evolution
adapters/                one subdirectory per source, each a small standalone service (+ importer CLI)
tests/                   engine unit tests + integration tests (Postgres, in-memory Qdrant)
jellyfin-plugin/          C#/.NET plugin, IHomeScreenSection-scoped
docs/                     architecture.md + recommender.md + installation.md + one guide per adapter
services/admin/           migrations, database roles, status, backups, forget/export, key rotation
muse                      the setup and operations tool (./muse setup, status, doctor, backup...)
SECURITY.md               what is encrypted, how access is limited, and what isn't covered
```

## Getting started

```bash
git clone <this repository> muse && cd muse
./muse setup      # checks, Jellyfin, hardware, secrets, start, verify
./muse status
```

Everything else (modes for local / LAN / Tailscale, importing history, backups, optional parts):
[`docs/installation.md`](docs/installation.md).

## Security

Muse aggregates a behavioural profile of your household, so it treats its own storage as hostile:
everything personal is encrypted by Muse itself (AES-256-GCM, with blind indexes for anything that
must stay searchable). A database dump, backup or copied volume reveals no titles, searches,
profiles or even library contents without the key file. Services run with least-privilege
database roles on segmented internal networks, as non-root read-only containers, with secrets
as files and no personal data in logs. Details and the honest limits: [`SECURITY.md`](SECURITY.md).

## License

[MIT](LICENSE)
