# Data security audit (Muse + Mono), November 2026

A focused review of how personal data moves through Muse and Mono after the query API, MCP
server, catalog, audio analyzer, context, groups and imports were added. Muse holds a behavioural
profile across music, films, videos, books and searches, plus things people told an assistant. That
makes it the most sensitive thing in a self-hosted stack, so the bar is "a leak of this hurts".

Scope: the code in this repository and in Mono's `server/`. Not covered: the host, Docker, the
network (see "Security model" in `architecture.md`), Jellyfin/Vesper themselves.

Severity: **High** (realistic path to someone else's data), **Medium** (needs a foothold, or leaks
less), **Low** (hardening). Status: **fixed** in this change, or **recommendation** (needs a decision
or lives outside the code).

## Summary

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Webhook and calendar URLs could make the scheduler call internal services (SSRF) | High | fixed |
| 2 | Recommendation API is open when `MUSE_READ_KEYS` is unset, and read keys aren't per person | High | warning added; recommendation |
| 3 | Link codes could be guessed without limit | Medium | fixed |
| 4 | Personal tokens never expired | Medium | fixed (optional expiry; Mono's assistant tokens: 365 days) |
| 5 | Imports accepted zip archives of unbounded unpacked size | Medium | fixed |
| 6 | Erasing a person left traces in group picks and notification keys | Medium | fixed |
| 7 | Tokens in query strings (Lidarr list, Mono streams) end up in access logs | Medium | recommendation |
| 8 | Adapter keys can act for any id that has no events from another source | Medium | documented; recommendation |
| 9 | Taste leaves the house: catalog lookups, previews, weather | Medium | documented; opt-out |
| 10 | Secrets at rest in plain text (ICS URLs, SoundCloud tokens, webhook secrets) | Medium | recommendation |
| 11 | An assistant can store sensitive facts | Low | mitigated (instructions, visible + deletable) |
| 12 | Who read what wasn't recorded | Low | fixed (access log) |

## Findings

### 1. SSRF through webhooks and calendars (High, fixed)

Any person with a write token could register a webhook or calendar URL; the scheduler then made
requests from inside `muse-net`. That's a path to Postgres, Qdrant, the internal APIs or a cloud
metadata endpoint (`169.254.169.254`).

Fix: `services/query-api/app/guards.py::outbound_url` resolves the host when the URL is saved and
refuses loopback, private, link-local, reserved and multicast addresses for non-admins. Admins can
still point household hooks at the LAN (Home Assistant). `MUSE_OUTBOUND_ALLOW_PRIVATE` lists
specific hosts that ordinary users may use too. Test: `tests/test_security.py`.

Residual: DNS rebinding (a name that resolves publicly at save time and privately later). To close
it fully, run the scheduler on a network that can't reach the internal services, or re-check at
send time.

### 2. Recommendation API access model (High, warning + recommendation)

`MUSE_READ_KEYS` unset means the recommendation API answers anyone who can reach it, and its
answers now include more than lists: `/context` (mood, activity, calendar kind), `/explain` (facts
such as "never suggest X"), `/group-recommendations` (per-member fit). Even with read keys, a key is
for a *client*, not a person: Vesper's key can ask about every user.

Done: a loud startup warning when no read keys are set. The default `docker-compose.yml` publishes
no ports, and the Vesper override binds to `127.0.0.1`.

Recommended:
- always set `MUSE_READ_KEYS` (one per client) and `MUSE_QUERY_READ_KEY` for the query API;
- give apps for people (assistants, other apps) the **query API** with personal tokens, never the
  recommendation API;
- keep the recommendation API reachable only from `muse-net` and the one host running Vesper.

### 3. Link-code guessing (Medium, fixed)

Codes are 8 characters from a 32-character alphabet (40 bits), single-use and valid for 15 minutes,
so guessing was already impractical. But redeeming one merges the redeemer into someone else's
profile (and so grants read access), and there was no limit. Now: 10 wrong codes per caller per
hour, then 429.

Note the social-engineering angle, which no limit fixes: someone who gets you to *redeem their
code* joins your identity to theirs. The UI says to only link accounts that are yours.

### 4. Token expiry (Medium, fixed)

`POST /v1/tokens` takes `expires_days`, and expired tokens are refused. Mono's "Connect an AI
assistant" now issues tokens valid for 365 days. Tokens are stored as SHA-256 hashes, are shown
once, can be read-only, and can be revoked (`DELETE /v1/tokens/{id}`).

### 5. Zip bombs in imports (Medium, fixed)

Spotify/Takeout exports arrive as zips. Archives that would unpack to more than 1 GB of JSON/CSV,
or that hold more than 2000 files, are refused. Zips inside zips are ignored, and single uploads
are capped at 300 MB.

### 6. Erasure completeness (Medium, fixed)

`DELETE /v1/users/{u}?confirm=true` removes:
- all of the person's ids;
- their events, impressions, facts, questions, goals, wanted items, context, calendars, tokens, webhooks, taste facets and models, overlay, presence, refs and imports;
- their Qdrant taste points.

It now also strips the person from `group_pick` (members and fits) and deletes notification keys
that contain their id. The access log keeps its rows but drops both ids. Content items (songs,
films) stay, since they aren't personal.

### 7. Tokens in URLs (Medium, recommendation)

Two flows carry a token in the query string, because the client can't send headers:
- the Lidarr-style list (`/v1/wanted/lidarr?token=`), which accepts a read-only token;
- Mono streams (`?t=`).

The stream token Muse uses (`MONO_MUSE_STREAM_TOKEN`) can only stream, which is tested. Muse's own
access log stores paths without query strings. Uvicorn's access log and any reverse proxy do log
query strings, though.

Recommended:
- run the query API with `--no-access-log`, or filter `token=`/`t=` in the proxy;
- use a dedicated read-only token for Lidarr and rotate it if logs leak.

### 8. Adapter acting for users (Medium, documented)

A source's ingestion key plus `X-Muse-User` lets that source (e.g. Mono) act for a person. This is
how Mono shows questions and mints assistant tokens without users ever seeing a Muse key. The
guard refuses ids that carry events from another source (Muse's own and imported events don't
count). An id with no events at all, though, can be claimed by any adapter.

Recommended: treat adapter keys like admin credentials (one per service, never shared, rotated
when a service is compromised). If that's too broad, a later change can bind adapter principals to
ids the adapter has already ingested events for.

### 9. Taste leaving the house (Medium, documented)

These are deliberate trade-offs, but worth knowing:
- **Catalog**: artist names from the household's taste go to MusicBrainz, ListenBrainz and Deezer. Your IP plus a list of artists is a fingerprint.
- **Audio analyzer**: fetches Deezer previews of discovered songs.
- **Scheduler**: sends the household's coordinates to Open-Meteo.

Nothing else (no user ids, no history, no facts) leaves.

Opt-outs:
- don't run `catalog` (and leave `MUSE_AUDIO_EMBEDDINGS` on for library songs only);
- leave `MUSE_WEATHER_LAT/LON` empty, or round them to one decimal (about 10 km);
- route outbound traffic through a VPN.

### 10. Secrets at rest (Medium, recommendation)

Some secrets are stored in plain text:
- ICS calendar URLs (which often embed a secret), in Postgres;
- webhook signing secrets, in Postgres, because they're needed to sign;
- SoundCloud tokens, in Mono's SQLite.

None of them are returned by any API after creation. The event store itself is the bigger prize.

Recommended: full-disk or volume encryption for Postgres, Qdrant and Mono's data directory (as
`architecture.md` already assumes), and backups encrypted the same way.

### 11. What assistants remember (Low, mitigated)

An assistant can call `remember` with anything. The MCP instructions limit facts to taste and
listening/viewing context and exclude health, politics, religion and relationships. Every fact
shows in Mono's Muse screen and in `GET /v1/users/me/facts`, with its origin, and can be deleted.
Calendar context stores only the kind of moment (workout, work...), never titles or attendees,
which is tested.

### 12. Access log (Low, fixed)

Every authenticated `/v1` call is recorded: who (token label, admin, or `adapter:<source>`), whose
data, method, path and status. People see their own log at `GET /v1/users/me/access-log`, and an
assistant can show it (`access_log` tool). Entries are kept 90 days (`MUSE_ACCESS_LOG_RETENTION_DAYS`).

## Things checked and found fine

- **SQL**: every query is parameterised. The few f-strings build table and column names from constants.
- **Secret comparisons**: admin token and stream token compare in constant time. Tokens and link codes are random (`secrets`) and stored hashed.
- **Webhooks**: signed (HMAC-SHA256 over the raw body, `X-Muse-Signature`) and retried with backoff; disabled after 50 failures.
- **Incognito**: the ingestion API drops a person's events while it's on, apart from `cataloged`, which carries no user signal.
- **Retention**: per-source policies, impressions after 180 days, expired context after a week, all enforced daily by the scheduler.
- **Multi-user separation**: personal tokens and adapter principals only reach their own (linked) person. Groups only for members. Tested in `tests/test_flow.py`, `test_situation.py`, `test_security.py`.
- **MCP over HTTP**: no auth of its own. Each call forwards the client's bearer token to the query API, which decides.
