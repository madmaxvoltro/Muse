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
- **Explains itself, and you can correct it**: every reason names the part of your profile
  behind it ("Similar to X", "Directed by Y"), and "not relevant" / "more" / "less" fixes the
  profile itself. A home screen of themed rows ("Because you watched X", "Watch again",
  "Something different") whose titles are their explanations.
- **Measurable**: an offline evaluator replays your own history and reports Recall/nDCG/
  diversity/calibration for every stage, so changes are judged by numbers.
- **User control overlay**: sliders, pins, an explore-dial, and per-recommendation explainability
  — sits on top of the automatic profile, never mutates it, always reversible.
- **Autonomous discovery/curator**: optionally lets Muse manage a storage budget (add *and*
  remove content) through Radarr/Sonarr. Never removes what someone watched recently, has on a
  list, asked for, loves or what you keep; a 48h "Soon Gone" window first; series start with
  season 1; real sizes and free-space checks; every action in the admin's Vesper feed with
  Keep and Remove now.
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

Core pipeline implemented and tested (`pytest tests` — unit tests for the engine, integration
tests against Postgres + in-memory Qdrant): event schema, ingestion API, embedding worker,
taste engine (`shared/muse_taste`, see `docs/recommender.md`), recommendation API, curator
(300GB budget + Soon Gone), and the SearxNG/Jellyfin/arr adapters. A local `docker compose build` succeeded for the Python
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
shared/muse_taste/      the taste engine: signals, profiles, retrieval, ranking, reranking, questions, eval
db/migrations/           Postgres/TimescaleDB schema
services/ingestion-api/  write-only endpoint all adapters POST events to
services/recommendation-api/  the 3-stage recommendation funnel
services/embedding-worker/    the taste engine's worker (catalog, profiles, questions) and the vector worker
                              (content/taste vectors, deep taste, learned ranker for the multi-space engine)
services/query-api/           the API for apps and assistants (search, profile, teach, context, groups, data)
services/mcp-server/          MCP server for AI assistants (query API tools + the taste engine's questions and profile)
services/catalog/             music beyond the library: public databases, new releases, wanted list
services/audio-analyzer/      listens to music: features + CLAP embeddings (CPU or GPU)
services/scheduler/           weather, calendars, webhooks, digest, goals, retention
services/curator/             autonomous storage-budget manager
adapters/                one subdirectory per source, each a small standalone service (+ importer CLI)
tests/                   engine unit tests + integration tests (Postgres, in-memory Qdrant)
jellyfin-plugin/          C#/.NET plugin, IHomeScreenSection-scoped
docs/                     architecture.md, recommender.md, installation.md, vesper.md (clients), api/ (API guide +
                          OpenAPI), security-audit.md, one guide per adapter
services/admin/           migrations, database roles, status, backups, forget/export, key rotation
muse                      the setup and operations tool (./muse setup, status, doctor, backup...)
SECURITY.md               what is encrypted, how access is limited, and what isn't covered
```

## Getting started

```bash
curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/Muse/main/install.sh | bash
```

That installs what's missing, then opens the setup in your browser (a QR code takes it to your
phone). At the end, scan the code in Vesper and you're connected. On Proxmox there are scripts
that make the container or VM for you. By hand: `git clone`, then `./muse setup` (terminal) or
`./muse setup --web`.

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
