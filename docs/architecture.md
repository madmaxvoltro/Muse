# Muse architecture

Muse is a self-hosted, privacy-first unified taste-profile and recommendation system. It combines
signal from Jellyfin, the arr-stack (Sonarr/Radarr/Prowlarr), a FreeTube-like YouTube frontend,
SearxNG, Open WebUI, Navidrome, and Audiobookshelf into one cross-domain taste profile, and serves
active recommendations primarily inside Jellyfin.

## Component overview

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  Jellyfin   │  │  arr-stack  │  │  FreeTube   │  │  SearxNG    │  │ Open WebUI  │  │ Navidrome/  │
│  adapter    │  │  adapter    │  │  adapter    │  │  sidecar    │  │  poller     │  │ Audiobookshelf│
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       └────────────────┴────────────────┴────────────────┴────────────────┴────────────────┘
                                          │  write-only, per-adapter API key
                                  ┌───────▼────────┐
                                  │ Ingestion API   │  ← validation, rate-limit, schema-enforcement
                                  └───────┬────────┘
                                          │
                          ┌───────────────▼────────────────┐
                          │  Postgres + TimescaleDB          │  ← event-store (append-only)
                          └───────────────┬────────────────┘
                                          │ (batch job reads new events)
                                  ┌───────▼────────┐
                                  │ Embedding worker │  ← local model, content + taste embeddings
                                  └───────┬────────┘
                                          │
                                  ┌───────▼────────┐
                                  │   Qdrant        │  ← vector store, similarity search
                                  └───────┬────────┘
                                          │
                          ┌───────────────▼────────────────┐
                          │      Recommendation API          │  ← 3-stage funnel, see below
                          └───────┬─────────────────┬──────┘
                                  │                   │
                          ┌───────▼───────┐   ┌───────▼────────┐
                          │ Jellyfin plugin │   │ Muse dashboard  │
                          │ (homepage rows) │   │ (mgmt/privacy)  │
                          └────────────────┘   └────────────────┘

                          ┌────────────────────────────────┐
                          │            Curator               │  ← autonomous storage-budget manager,
                          │  (TMDB/Trakt candidates + owned   │     calls Radarr/Sonarr API only —
                          │   item removal scoring)           │     never touches files directly
                          └────────────────────────────────┘
```

**Core security principle:** the event store is the single highest-value target in the whole
system — a compromise there exposes an aggregated behavioral profile, not just one source. Adapters
are therefore write-only (they can never read back from the ingestion API), each has its own
API key, and everything runs on its own network segment with no direct external exposure. See
"Security model" below.

## Event schema

Single append-only table (`db/migrations/001_init_event_store.sql`), all adapters write into
this shape (mirrored in `shared/muse_schema/event.py`):

| field | notes |
|---|---|
| `user_id` | present from day 1, even single-user — see Multi-user below |
| `source` / `source_item_id` | which adapter, and that adapter's own ID for the item |
| `item_type` | movie / series_episode / youtube_video / track / audiobook / ebook / search_query / chat |
| `action` | played / completed / paused / **skipped / rejected** / favorited / searched / added_watchlist / removed |
| `action_weight` | negative signals (skipped/rejected) are first-class from v1, not bolted on later |
| `metadata` | jsonb, source-specific (SearxNG: query+engine; Jellyfin: genre+cast; ...) |

Append-only — never mutated except an explicit GDPR-style per-source delete. This also doubles as
an audit/anomaly-detection base (see Security model).

## Recommendation algorithm — 3-stage funnel

Not a single similarity search. Chosen deliberately for "Google-level" recommendation quality:

1. **Candidate generation** (~200 candidates, optimizes recall): multiple parallel generators —
   Qdrant vector similarity, item-item co-occurrence within the user's own history (substitutes
   for collaborative filtering since it's effectively single-household), cross-source backlog
   signal (searched-but-not-watched), trending/new-arrival injection for cold-start.
2. **Ranking** (optimizes precision): every candidate scored individually with a **hand-weighted,
   explainable scoring function** (similarity + recency-decay + within-user popularity +
   completion-rate of similar content + confidence) — not a learned model, chosen for
   explainability and because a learned model needs feedback volume Muse won't have early on.
3. **Re-ranking/policy**: diversity constraints (cap per genre/franchise), explore-dial allocation,
   user-overlay controls applied here, confidence affecting placement.

**Confidence is a first-class scoring input, not just a UI label.**
`confidence = f(interaction count with similar content, variance, signal age, cross-source agreement)`.
High-confidence+high-score → shown as a strong match. Low-confidence+high-score → deliberately
routed into the explore slots rather than dropped, so the system actively collects feedback on
uncertain-but-promising items (lightweight multi-armed-bandit pattern).

## User control overlay

A **separate layer on top of the automatic, behavior-based taste vector** — never writes into the
profile itself, so it's always reversible back to "fully automatic". Includes:

- Per-source/per-type weight sliders (maps onto `action_weight`)
- "Forget this" per item or time range
- Mood-mode presets (temporary override)
- Explore-dial (user-adjustable explore/exploit %)
- Pin/boost a topic manually
- Confidence shown per recommendation (also feeds ranking, not just display)
- Direct thumbs up/down, applied immediately to `action_weight`
- Toggle a data source off temporarily to A/B recommendations

## Discovery/acquisition pipeline

Scores content Muse doesn't own yet, sourced from TMDB (primary) + Trakt.tv (trending signal).
**Muse never touches files directly** — it only calls Radarr/Sonarr's API to add/remove; the
arr-stack remains the sole thing that touches disk.

**Autonomous storage-budget curator**: manages a fixed 300GB quota autonomously — adds new
high-scoring content and removes existing low-scoring content, no manual approval gate. Given this
is the most irreversible piece of Muse, it ships with mandatory safety nets:

- **"Soon Gone" grace mechanism**: a removal candidate is placed in a Jellyfin collection
  called "Soon Gone" with a 48h countdown (tracked in the `soon_gone` table, not in Jellyfin).
  If untouched after 48h, Muse sends a remove command to Radarr/Sonarr.
- **Whitelist-by-watching**: opening/starting playback of a "Soon Gone" item within the window
  (caught by the Jellyfin plugin's playback-start hook) whitelists it immediately — pulled out of
  the auto-managed quota permanently. Playback-start alone counts, no minimum watch-duration.
- **Always excluded from auto-delete**: favorited/pinned items, and anything partially watched
  (>10% and <90%).
- **Rate limits**: 10 auto-downloads/day per user-profile, scaled down dynamically for
  rarely-active profiles (rolling ~14-day activity window) so an idle/guest profile doesn't burn
  shared quota. The 300GB quota itself is the global ceiling — no separate flat global number.
- **Multi-profile combination**: ADD decisions use the combined/averaged taste score across active
  profiles. REMOVE decisions require a low score from *all* active profiles — if any single
  profile's Soon-Gone whitelist triggers, the item is saved for everyone, not just that profile.

Server has 10TB total capacity; the 300GB auto-curated quota is a small, comfortable slice of that.

## Multi-user / auto-profile creation

Jellyfin users are auto-imported as Muse profiles (via Jellyfin's user API), 1:1 mapped to
`user_id`. Other sources have per-person accounts too, but usernames aren't guaranteed to match
exactly across services.

- **Identity resolution** (`identity_mapping` table): each adapter event carries the source-native
  username. Exact matches resolve automatically; fuzzy matches are only ever *suggested* — never
  auto-committed — and require one-time manual confirmation in the dashboard. This is
  privacy-critical: a wrong auto-match would misattribute one household member's data to another.
- **Cold-start profiles** (e.g. a profile with only Jellyfin data) are handled entirely by the
  existing confidence mechanism — no separate cold-start logic needed.

## Jellyfin plugin scope

Deliberately scoped to the official `IHomeScreenSection` plugin API (Jellyfin 10.9+) rather than
forking `jellyfin-web` — no client fork to maintain across Jellyfin updates. The plugin only adds
rows to the existing homepage/library views:

- "Recommended for You" (with confidence/explainability)
- "Continue based on taste"
- "Soon Gone"

Jellyfin's native "Recently Added"/"Continue Watching" rows are untouched. Per-library Movies/TV/
Anime separation is already native to Jellyfin (library views) — no custom top-level layout needed.

## Security model

Threat model: the main risk is **lateral movement within the home network**, not external
attackers (Tailscale/VLAN already covers that). Principles:

- Muse runs on its own VLAN segment, isolated from the rest of the homelab.
- Encryption at rest (LUKS) on the Muse DB volume.
- No direct external exposure — Tailscale-only access, no ports published to the host.
- Adapters are write-only with minimal, per-adapter credentials (see `services/ingestion-api/app/auth.py`)
  — a compromised adapter can inject rate-limited bad events, but can never read the event store back.
- The vector store (Qdrant) is secured at the same level as raw data — embeddings can leak/reconstruct
  sensitive signal, they are not "safe just because they're numbers."
- Backups (Proxmox Backup Server) must carry the same encryption requirement as the live DB.
- Anomaly detection on the event store itself (unusual query volume/time) is a candidate future
  feature — an IDS-like layer on top of the append-only log.
