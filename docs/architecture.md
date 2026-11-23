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

Full design, the research each stage builds on, and how to evaluate it:
[`recommender.md`](recommender.md). In short:

1. **Candidate generation** (recall): eight generators fused with Reciprocal Rank Fusion:
   one ANN query per *interest* (multi-interest profiles after PinnerSage instead of one
   averaged taste vector), short-term intent, item-to-item from your strongest titles, TMDB
   "fans also liked" (collaborative signal a single household can't produce itself),
   household CF, backlog (watchlist/requested), mood text, and quality for cold start.
2. **Ranking** (precision): an explainable linear model over named features (interest
   affinity, exact tag matches such as director/keywords, repulsion from disliked titles,
   related support, intent, Bayesian rating, freshness, fatigue...). It started hand-weighted
   and is re-fitted daily from logged impressions, with a prior that keeps the hand-set
   weights until there's enough data. Every candidate also gets an uncertainty from how much
   judged evidence sits near it.
3. **Re-ranking/policy**: the list is calibrated to your interest proportions (Steck 2018),
   penalises redundancy (MMR), keeps one entry per franchise (the next unseen part), and
   fills explore slots by Thompson sampling over uncertain-but-promising candidates. User
   overlay controls apply here and in signal aggregation.

**Confidence is a first-class scoring input, not just a UI label.** It's computed per
candidate from nearby evidence and its agreement. Low-confidence + high-score items are
routed into explore slots on purpose: showing them is how Muse learns.

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

**Linked identities (implemented).** One person is one canonical `user_id` plus any number of
aliases (`user_alias`): the Jellyfin GUID, the Mono account, a SearxNG id. Linking is explicit,
never inferred: a single-use code made on one account and redeemed on the other (query API /
MCP / Mono's settings), or an admin call. Events keep the id they were written with — the store
stays append-only and unlinking is a row delete — and readers resolve at read time
(`shared/muse_schema/identity.py`): the embedding worker builds one taste vector per person over
all their ids, the recommendation API excludes what any of their ids has seen, profiles and
search cover them all.

## What Muse is told (facts, questions)

Behaviour is ambiguous; people and their assistants can say what it means. Three tables
(`db/migrations/007_people_and_knowledge.sql`), all written through the query API:

- **Facts** (`profile_fact`): "loves slow Dutch ballads", "never suggest musicals". Facts with a
  sentiment are embedded and averaged into the taste vector like items (so a fact can create a
  domain's vector from nothing — cold start by interview); facts that name a `subject` are also
  applied exactly at ranking time (`avoid` filters, `likes`/`dislikes` move the score, and the
  reason shows up in `why`). Retracting one undoes it on the next worker cycle.
- **Questions** (`question`): Muse generates them where behaviour can't answer — a domain the
  household uses but this person hasn't touched, genres/artists with mixed signals, uncertain
  recommendations, how adventurous to be, whether one domain's taste may steer another.
  Options carry effects (a `rated` event, a fact, an overlay setting). Assistants ask them in
  conversation via MCP; Mono shows them in its Muse screen.
- **Feature suggestions** (`feature_suggestion`): assistants propose improvements to Muse
  itself while helping someone; duplicates become votes.

The overlay gained `preferences.taste_blend`: how much of the cross-domain (`_combined`) taste is
mixed into each domain's own, and the recommendation API accepts `taste_from=<domain>` to rank one
domain with another's taste (music taste → films).

## Query API and MCP

`services/query-api` is the read-and-teach API for apps and assistants: search (semantic +
keyword, personalised), profile, history, recommendations, facts, questions, identities, tokens.
Per-person bearer tokens limit an assistant to one person; a source server can act for its own
users with its ingestion key. `services/mcp-server` exposes it as MCP tools (stdio or shared
streamable HTTP), with instructions that keep assistants to taste-only facts, one question at a
time, and one person's data per conversation.

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

Threat model and implementation: [`../SECURITY.md`](../SECURITY.md). In short: Muse encrypts
every personal field itself (AES-256-GCM; blind HMAC indexes for searchable identifiers), so
dumps, backups and copied volumes are unreadable without the key file; adapters are write-only
with per-adapter keys; every service has its own least-privilege database role; data stores sit
on an internal network with no internet; the APIs listen on localhost (HTTPS proxy for other
machines); secrets are Docker secret files; containers run non-root and read-only; logs carry no
personal data. Remaining exposure (activity timestamps, catalog vectors, a compromised running
host) is documented there.

## Understanding taste (beyond metadata)

Text embeddings of titles, genres and lyrics only know what tags say. Muse also uses:
- **Sound**: the audio analyzer decodes parts of each track (intro, body, outro; or a 30 s
  preview) and computes tempo, key/Camelot, energy, danceability, acousticness... plus a CLAP
  embedding of how it sounds. Taste in sound is averaged from what is finished/loved vs skipped.
- **Facets**: spherical k-means per person and domain, in text and sound space, so a person with
  two tastes gets two centres; each facet gets room in every list. Sound facets are named from a
  descriptor vocabulary embedded with CLAP's text encoder (the taste portrait).
- **Behaviour**: items played together in sessions (PPMI + SVD) and A->B transitions with how they
  ended; relations that no metadata contains.
- **Preference models**: logistic regression on a person's likes vs skips over audio features,
  kept only when it predicts on held-out data.
- **Context tastes**: per daypart, weekend, device class.

## The multi-space engine

Muse has two engines behind one API. The **taste engine** (`shared/muse_taste`,
`services/recommendation-api/app/engine.py`, [`recommender.md`](recommender.md)) serves films and
series from canonical titles with calibrated lists, taste evolution and questions. The
**multi-space engine** (`services/recommendation-api/app/multispace.py`, its routes in
`routes_multispace.py`, fed by `services/embedding-worker/vector_worker.py`) serves music,
YouTube and books, next-track and radio, groups and context-aware lists; `/recommendations`
picks the engine by item type and parameters (`engine=` forces one). Vesper's rows use the
multi-space vectors too (`funnel.py`, see [`vesper.md`](vesper.md)).

`multispace.py`: generators (taste, facets, sound, behaviour,
co-occurrence, new arrivals, mood, goals) -> features in every space -> hand weights or a learned
ranker (activated only when it beats them on holdout, trained from logged impressions and their
outcomes) -> facts, ignored-impression penalty -> re-rank with facet quotas, artist/genre caps,
explore slots. `/next` and `/radio` score transitions (sound, Camelot key, tempo incl. half/double
time, energy arc, played-after) with beam search. `/group-recommendations` combines per-member
scores (mean + least misery, fairness memory, vetoes).

## Outside the library, and outside the house

The catalog worker uses MusicBrainz (ids, genres, release groups), ListenBrainz (listening
similarity) and Deezer (related artists, previews, BPM): catalog tracks become recommendable as
discoveries, new releases of loved artists and good discoveries become the wanted list. What
leaves the house is listed in `docs/security-audit.md`.
