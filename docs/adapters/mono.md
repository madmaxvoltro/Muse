# Mono setup

[Mono](https://github.com/madmaxvoltro/mono) is a self-hosted music server and app. It talks to
Muse directly; there is no separate adapter container. Status: implemented.

## What Mono sends

All as `source: "mono"`, `item_type: "track"`, `source_item_id` = Mono's track id, user id =
Mono's per-account Muse id. Every event carries `metadata.dedupe_key`, so Mono can re-send
freely (history backfill, restarts) without double counting.

| Mono | Muse action |
|---|---|
| every track in the library (rich metadata, start of the lyrics) | `cataloged` |
| a play started / how it ended | `played`, `completed`, `skipped` (with progress) |
| like / unlike | `favorited` / `unfavorited` (cancels the like's weight exactly) |
| song put in a playlist (by hand or by an import) | `added_to_playlist` |
| a search, once the person settled on the query | `searched` (`item_type: search_query`) |
| an imported Spotify/SoundCloud like or playlist song that isn't in the library | `favorited` / `added_to_playlist` on `wish:<key>`, `in_library: false` |

Mono derives these from its own tables every few seconds (`sync()` in `server/mono/muse.py`),
so history from before Muse was connected is sent once on first start, and nothing an import or
a later rematch does is missed. Events queue in an outbox while Muse is down.

## What Mono reads

* `GET /recommendations/{user}?item_type=track&source=mono` for "Recommended for you" (falls back
  to Mono's own recommender when Muse doesn't answer), with Muse's `why` as the reason.
* The [query API](../../services/query-api/README.md), as the signed-in user (adapter key +
  `X-Muse-User`): the app's **Settings → Muse** screen shows Muse's questions, what Muse was
  told (with forget), linked accounts and link codes, and hands out a personal token plus MCP
  config for an AI assistant.

## Configure

Muse `.env`: add `mono:<key>` to `MUSE_ADAPTER_KEYS` (generate with `openssl rand -hex 32`).
If the recommendation API has `MUSE_READ_KEYS`, add one for Mono too.

Mono server environment:

```bash
MONO_MUSE_INGEST_URL=http://127.0.0.1:8771     # ingestion-api
MONO_MUSE_API_KEY=<the mono key>
MONO_MUSE_REC_URL=http://127.0.0.1:8770        # recommendation-api
MONO_MUSE_READ_KEY=<a read key, if Muse requires them>
MONO_MUSE_QUERY_URL=http://127.0.0.1:8772      # query-api: questions, linking, assistants
# How an assistant's machine reaches Muse (shown in the app's MCP config):
MONO_MUSE_PUBLIC_QUERY_URL=http://muse.your-tailnet:8772
MONO_MUSE_MCP_URL=http://muse.your-tailnet:8773/mcp   # optional, if the shared MCP endpoint is exposed
```

## One person, one profile

Mono's account and your Jellyfin user are different ids in Muse. Link them once so your music
taste and your film taste are one profile (and "music taste → films" works): in Mono, Settings →
Muse → "Link another account" gives a code; redeem it from the other side (an assistant
connected with a Jellyfin-side token: `link_account`, or the admin:
`POST /v1/identity/aliases`). Or the other way round: create the code elsewhere and enter it in
Mono.

## Verify

```bash
docker compose exec postgres psql -U muse -d muse -c \
  "SELECT action, count(*) FROM event WHERE source = 'mono' GROUP BY 1;"
```

Before Muse knew the `mono` source, its ingestion API rejected Mono's events (422) and Mono
dropped them. After upgrading both, Mono's first sync re-sends the full history.
