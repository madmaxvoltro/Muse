# Query API

The read-*and-teach* side of Muse, for apps and AI assistants (the MCP server in
`services/mcp-server` is a thin client of this API). Port 8002 in the container
(`127.0.0.1:8772` with `docker-compose.vesper.yml`).

## Auth

| Caller | How | Acts for |
|---|---|---|
| Household admin | `Authorization: Bearer $MUSE_ADMIN_TOKEN` (or a token minted with `user: null`) | anyone; pass `user=<id>` |
| A person | `Authorization: Bearer muse_...` (minted via `POST /v1/tokens`) | that person and their linked accounts |
| A source server (e.g. Mono) | `X-Muse-Api-Key: <its ingestion key>` + `X-Muse-User: <its user id>` | that user, only if the id has no events from another source |

Tokens can be read-only (`can_write: false`). Only hashes are stored.

## Endpoints

`user` is `me` or a user id. Everything that reads a person covers all their linked ids.

| | |
|---|---|
| `GET /v1/me`, `GET /v1/users`, `GET /v1/users/{user}/identity`, `PUT /v1/users/{user}` | who's who, display names |
| `GET /v1/search?q=&item_type=&source=&user=&personalize=` | semantic + keyword search over everything Muse has seen, ranked by taste, with `you` (what the person did with each hit) |
| `GET /v1/items/{source}/{id}`, `GET /v1/users/{user}/similar?source=&source_item_id=` | details, "more like this" (also across domains) |
| `GET /v1/users/{user}/profile?domain=&days=` | loved/disliked genres, top artists, favourites, facts, settings, identities, recent activity |
| `GET /v1/users/{user}/history` | events, newest first |
| `GET /v1/users/{user}/recommendations?item_type=&taste_from=&blend=&explore_ratio=&source=` | the funnel, with titles and human `why`; `taste_from=track` ranks films by music taste |
| `POST /v1/users/{user}/feedback` | explicit rating (-1..2) on an item → `rated` event |
| `GET/POST /v1/users/{user}/facts`, `DELETE .../facts/{id}` | what Muse was told (shapes the taste vector; `avoid` facts filter) |
| `GET/PUT /v1/users/{user}/settings` | explore ratio, cross-domain `taste_blend`, source weights |
| `GET/POST /v1/users/{user}/questions`, `POST /v1/questions/{id}/answer`, `.../skip` | Muse's questions; answering applies their effects |
| `POST /v1/identity/link-codes`, `POST /v1/identity/link`, `POST/DELETE /v1/identity/aliases` | join accounts of one person |
| `POST/GET/DELETE /v1/tokens` | personal (or household) tokens |
| `GET/POST /v1/suggestions`, `PUT /v1/suggestions/{id}` | feature ideas for Muse, deduplicated into votes |
| `GET /v1/capabilities` | which sources feed Muse, which aren't connected |

## How teaching reaches recommendations

* **Facts** with a sentiment are embedded by the embedding worker and averaged into the taste
  vector like items (weight `sentiment × confidence × MUSE_FACT_WEIGHT`, default 2). A
  domain-specific fact can create that domain's taste vector on its own, so a short interview
  fixes cold start. `general` facts shape every domain.
* Facts with a `subject` are also applied precisely in the recommendation API: `avoid` drops
  matching items, `likes`/`dislikes` add a `stated_preference` term to the score (and to `why`).
* **Answers** apply the chosen option's effects: a `rated` event, a fact, or a setting.
* **Linking** accounts makes the worker recompute one vector per person over all their ids.

## Questions Muse generates

Cold start per domain the household uses; genres/artists with mixed signals; uncertain
recommendations ("Would you be into X?"); how adventurous to be; whether music taste may steer
other domains. Assistants can queue their own (`POST .../questions`); apps show them too.

## Semantic search

Uses the same sentence-transformers model as the embedding worker (`MUSE_EMBEDDING_MODEL`).
Without it (`MUSE_QUERY_EMBEDDER=none`) search is keyword-only.

## Tests

From the repo root, against a scratch Postgres 16 (TimescaleDB optional):

    MUSE_TEST_DATABASE_URL=postgresql://muse@localhost:5432/muse_test pytest

The suite runs the embedding worker, recommendation API, query API and MCP server together
(in-memory Qdrant, a fake embedding model). **It drops and recreates the `public` schema of
that database.**
