# Query API

One API for apps and assistants to ask Muse about a person and to tell it things. It runs next
to the two existing APIs (`services/query-api`, port `8772` on `MUSE_BIND`, `/query/` behind
the TLS proxy) and doesn't replace them:

| reads | the recommendation API (recommendations, profiles, the question queue) |
|---|---|
| answers | the ingestion API `/answers` (the single write path for taste data) |
| its own tables | people, linked ids, tokens, facts, goals, mood/context, settings, ideas (migration 011) |

Interactive reference: `http://127.0.0.1:8772/docs` (OpenAPI at `/openapi.json`).

## Authentication

| caller | how | may act for |
|---|---|---|
| a person (assistant, MCP) | `Authorization: Bearer <token>` | that person, with all linked ids |
| an app or server (Mono, Navidrome...) | `X-Muse-Api-Key: <its ingestion key>` + `X-Muse-User: <uuid>` | its own users only: an id with events from another source is refused (403) |
| the household admin | `Authorization: Bearer <admin_token>` (`./muse keys`) or a household token | anyone, with `?user=<id>` |

Tokens are random (`muse_...`), shown once, stored only as a keyed digest, expire after a year
by default and can be revoked. Read-only tokens (`can_write: false`) can't change anything.
Every person-endpoint takes `?user=me` (default) or a UUID (admin, or one of your own linked ids).

## Endpoints

People and access

| | |
|---|---|
| `GET /v1/me` | the person: canonical id, display name, every linked id with its events per source |
| `GET /v1/users` | admin: everyone, with linked ids |
| `PUT /v1/users/me/name` | `{display_name}` |
| `POST /v1/tokens` | `{label, display_name?, can_write?, expires_days?}` -> `{id, token}`; admin `?household=true` |
| `GET /v1/tokens`, `DELETE /v1/tokens/{id}` | list (labels only) / revoke |
| `POST /v1/identity/link-codes` | a one-time code, valid 15 minutes |
| `POST /v1/identity/link` | `{code, label?}`: the caller's id (and everything linked to it) joins the code's owner |
| `DELETE /v1/identity/aliases/{id}` | unlink again |

Linking never rewrites history: events keep the id they were written with and every reader
(worker, recommendation API, ingestion) resolves linked ids to one person. Linking a Mono
account to a Jellyfin account makes one taste profile out of both and lets Mono's Muse screen,
questions and recommendations be the same person's.

What Muse is told

| | |
|---|---|
| `GET/POST /v1/users/me/facts`, `DELETE /v1/users/me/facts/{id}` | `{statement, kind: likes/dislikes/avoid/context/fact, domain, confidence}` |
| `GET/POST /v1/users/me/goals`, `DELETE /v1/users/me/goals/{id}` | `{title, target, domain}`; progress = different titles finished or loved since |
| `POST /v1/suggestions`, `GET /v1/suggestions` (admin) | ideas for Muse itself |

`likes` facts become interests of their own in that domain's taste profile on the worker's next
cycle (embedded like a pinned topic): "loves slow piano ballads" pulls such songs up before
anything like it was played. Everything told is visible to the person and deletable.

Questions

| | |
|---|---|
| `GET /v1/users/me/questions?limit=3&domain=music` | the queue as simple cards: `{id, kind: "choice", muse_kind, prompt, options: [{value, label}], reason, item, context}`; `raw=true` for the recommendation API's format (onboarding grids) |
| `POST /v1/questions/{id}/answer` | `{option, text?}` (`text` is kept as a fact), or `{answer: {...}}` with the exact `/answers` body |
| `POST /v1/questions/{id}/skip` | closes it; nothing is learned |

Context and privacy

| | |
|---|---|
| `GET /v1/users/me/context` | `{signals: [{kind, value, until}]}`; expired entries are left out |
| `POST /v1/users/me/context` | `{kind, value, ttl_minutes}`; `kind: "mood"` with `{mood: "..."}` is used by recommendations that name no mood; other kinds (device, weather, calendar, home, activity, company) are kept for clients and assistants |
| `DELETE /v1/users/me/context/{kind}` | clear one |
| `PUT /v1/users/me/incognito` | `{minutes}`: while it lasts the ingestion API drops what this person plays (all linked ids); 0 ends it |
| `GET /v1/users/me/settings` | `{preferences}` (e.g. `incognito_until`) |

Taste

| | |
|---|---|
| `GET /v1/users/me/taste-portrait?item_type=track` | per domain: summary, `sound` (interests with share, examples, trend), loved, disliked, stats, narrative |
| `GET /v1/users/me/recommendations?item_type=track&mood=` | the recommendation API's explained picks for this person |
| `GET /v1/users/me/releases` | new releases by loved artists (empty until the music catalog service lands) |

## Data at rest

Free text about a person (fact statements, goal titles, the mood and context, display names,
alias and token labels, suggestions) is sealed with the master key like every other personal
column; `./muse rotate-keys` reseals it, `./muse forget-user` deletes it and `./muse export-user`
includes it. The API logs no request URLs. Its database role (`muse_query`) can read only the event
columns it needs (no metadata) and can't write events or profiles.

## Examples

```sh
# Mono (or any app) acting for one of its users
curl -H "X-Muse-Api-Key: $MONO_MUSE_API_KEY" -H "X-Muse-User: $UID" http://127.0.0.1:8772/v1/users/me/questions

# a token for an assistant, then use it
curl -X POST -H "Authorization: Bearer $ADMIN" "http://127.0.0.1:8772/v1/tokens?user=$UID" -d '{"label":"assistant"}' -H 'content-type: application/json'
curl -H "Authorization: Bearer $TOKEN" -X POST http://127.0.0.1:8772/v1/users/me/facts \
     -H 'content-type: application/json' -d '{"statement":"Can'\''t stand autotune","kind":"dislikes","domain":"music"}'
```

## Assistants (MCP)

`services/mcp-server` in personal mode talks to this API with the person's token:
`pip install ./services/mcp-server` gives `muse-mcp`; run it with `MUSE_QUERY_URL` and
`MUSE_TOKEN`, or point an MCP client at the server's `/mcp` with its own
`Authorization: Bearer <token>` header. The `me_*` tools ask questions, remember what the user
says, set mood and context, read the portrait and goals and suggest features.
