# Muse API

Everything Muse does is reachable over HTTP, so any app can use the household's taste: a
player, a TV client, Home Assistant, a download tool, an assistant. This guide explains the
concepts and shows each part with examples. The exact schemas are in the OpenAPI files next to
this one, and every running service serves interactive docs at `/docs` (Swagger) and `/redoc`.

| Service | Who calls it | Port (container / Vesper override) | Spec |
|---|---|---|---|
| **Query API** | apps for people, assistants, integrations | 8002 / `127.0.0.1:8772` | [`query-api.openapi.json`](query-api.openapi.json) |
| **Recommendation API** | trusted players (Mono, Vesper, Jellyfin plugin) | 8001 / `127.0.0.1:8770` | [`recommendation-api.openapi.json`](recommendation-api.openapi.json) |
| **Ingestion API** | sources sending what people do | 8000 / `127.0.0.1:8771` | [`ingestion-api.openapi.json`](ingestion-api.openapi.json) |
| **MCP server** | AI assistants (Claude, Open WebUI, ...) | 8003 / `127.0.0.1:8773` | [`../../services/mcp-server/README.md`](../../services/mcp-server/README.md) |

**Start with the Query API.** It covers everything (it proxies recommendations, radio, groups
and explanations), it has per-person auth, and it logs who read what. Use the Recommendation API
directly only from trusted server-side players.

## Contents

1. [Concepts](#concepts)
2. [Authentication](#authentication)
3. [Know: profile, portrait, history, search](#know)
4. [Recommend: lists, radio, next, groups, why-not](#recommend)
5. [Teach: feedback, facts, questions, settings, goals](#teach)
6. [Right now: context and moods](#right-now)
7. [Groups and Home Assistant](#groups-and-home-assistant)
8. [Music beyond the library: releases and wanted](#music-beyond-the-library)
9. [People and accounts: linking, tokens](#people-and-accounts)
10. [Your data: imports, export, erase, incognito, access log](#your-data)
11. [Webhooks](#webhooks)
12. [Sending events (sources and apps)](#sending-events)
13. [Recipes](#recipes)
14. [Errors, limits, versioning](#errors-limits-versioning)

---

## Concepts

- **Event**: something a person did with an item: `played`, `completed`, `skipped`, `favorited`,
  `rated`, `added_to_playlist`, `searched` and so on. Each event has a source (`mono`, `jellyfin`,
  `navidrome`, ...), the source's own item id, an `item_type` (`track`, `movie`, `series`,
  `series_episode`, `youtube_video`, `audiobook`, `ebook`) and optional `device_context`. The event
  store is append-only.
- **Item**: identified by `(source, source_item_id)`. Muse knows an item from its metadata (text
  embedding), its sound (CLAP embedding plus features such as tempo, key and energy, for music),
  and how people use it (behaviour embedding, transitions).
- **Person**: one canonical `user_id` plus any linked ids (their Jellyfin user, Mono account,
  Navidrome user, ...). Everything about "a user" covers all their ids. In paths, `me` means the
  caller.
- **Taste**: per person and domain, in three spaces (metadata, sound, behaviour), split into
  **facets** (the distinct sides of their taste), with **context tastes** (night, weekend, TV, car)
  and a **preference model** (what they pick within a style). The embedding worker recomputes it
  every few minutes, and the deep parts hourly.
- **Facts and questions**: what people (or their assistants) told Muse, and what Muse wants to ask.
  Facts shape taste and filter recommendations; answering a question applies its effect.
- **Context**: short-lived signals such as mood, activity, device, weather, calendar kind and
  presence. They change recommendations for a while, never the profile.
- **Catalog**: music that exists but isn't in the library (MusicBrainz, ListenBrainz, Deezer). It
  feeds discovery, new releases and the wanted list.

## Authentication

| Header | Who | Can |
|---|---|---|
| `Authorization: Bearer muse_...` | a **personal token** | act for that person (and their linked ids); read-only tokens only read |
| `Authorization: Bearer $MUSE_ADMIN_TOKEN` (or a household token) | household admin | act for anyone; pass `user=<id>` |
| `X-Muse-Api-Key: <ingestion key>` + `X-Muse-User: <id>` | a source server (Mono) | act for its own users (ids with no events from another source) |
| `X-Muse-Read-Key` | trusted players, on the **Recommendation API** | read recommendations for anyone |

Getting a personal token:
- **From Mono**: Settings → Muse → "Connect an AI assistant".
- **As admin**:
  ```bash
  curl -X POST $MUSE/v1/tokens -H "Authorization: Bearer $MUSE_ADMIN_TOKEN" -H 'content-type: application/json' \
       -d '{"user": "<user id>", "label": "Kitchen tablet", "can_write": false, "expires_days": 365}'
  ```

Tokens are shown once, stored hashed, can expire, and are revoked with `DELETE /v1/tokens/{id}`.
`GET /v1/me` tells you who a token is.

In the examples below, `$MUSE=http://127.0.0.1:8772` and `H='Authorization: Bearer muse_...'`.

## Know

```bash
curl -H "$H" "$MUSE/v1/users/me/profile?domain=track"   # loved/disliked genres, top artists, favourites, facts, settings
curl -H "$H" "$MUSE/v1/users/me/taste-portrait"         # the sides of your taste, named by how they sound
curl -H "$H" "$MUSE/v1/users/me/history?item_type=movie&limit=20"
curl -H "$H" "$MUSE/v1/users/me/year-review?year=2026"
```

**Search** across everything Muse has seen, by meaning and keyword, ranked by your taste. Each hit
says what you already did with it (`you`):

```bash
curl -H "$H" "$MUSE/v1/search?q=slow-burn%20sci-fi&item_type=movie&limit=10"
```

**Search by sound** (music, CLAP):

```bash
curl -H "$H" "$MUSE/v1/search/sound?q=dreamy%20synths,%20slow,%20female%20vocals"
```

**Items and "more like this"**: the second call can cross domains (`item_type=movie` from a song):

```bash
curl -H "$H" "$MUSE/v1/items/mono/<track id>"
curl -H "$H" "$MUSE/v1/users/me/similar?source=mono&source_item_id=<id>&item_type=movie"
```

## Recommend

```bash
curl -H "$H" "$MUSE/v1/users/me/recommendations?item_type=track&limit=20&source=mono"
```

| Parameter | Meaning |
|---|---|
| `source` | only items from these sources: `mono` (playable in Mono), `catalog` (not in the library yet) |
| `mood` | a preset (`focus`, `chill`, `workout`, `party`, `sleep`, `sad`, `happy`, `cooking`, `rainy`, `light`, `intense`, `cozy`, ...) or a few words |
| `taste_from` | rank one domain with another's taste: `item_type=movie&taste_from=track` |
| `blend` | 0 to 1, how much cross-domain taste to mix in |
| `explore_ratio` | 0 to 1, share of deliberate wildcards |
| `tz` | the person's IANA time zone, for time-of-day context |

Each item carries `score`, `confidence`, `explore`, `facet` (which side of your taste it serves),
`generators` (how it was found), `explanation` (every weighted term of the score), `why` (readable
reasons), and `audio` (tempo, key, energy) for music. Responses also carry a `request_id`: Muse
logs what it showed, and that is how it learns from lists.

**Radio** is a session that fits you and flows. Arc is one of `steady`, `build`, `wind_down` or
`peak`; `discovery` runs from 0 (favourites) to 1 (only new):

```bash
curl -H "$H" "$MUSE/v1/users/me/radio?seed_source=mono&seed_id=<id>&length=25&arc=wind_down&playable_source=mono"
```

Each track has `transition`, which shows how well it follows the previous one:
- `key` (Camelot compatibility)
- `tempo` (half or double time counts)
- `energy` (against the arc)
- `sound`
- `after` (how often it followed here)
- `taste`

**Next up** is the same scoring for one step:

```bash
curl -H "$H" "$MUSE/v1/users/me/next?source=mono&source_item_id=<id>&n=3&history=mono:<id1>&history=mono:<id2>"
```

**Why not?** answers why something isn't recommended:

```bash
curl -H "$H" "$MUSE/v1/users/me/why-not/jellyfin/<item id>"
# {"recommendable": false, "blockers": ["filtered by something you told Muse: Never suggest musicals"], "rank": ..., "detail": {...}}
```

Groups are covered under [Groups and Home Assistant](#groups-and-home-assistant).

## Teach

```bash
# A thumbs: -1 not for me, 0 clear, 1 good, 2 love it
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/feedback" \
     -d '{"source": "jellyfin", "source_item_id": "<id>", "rating": 2}'

# Steer harder (a rating plus a lasting preference)
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/more-like-this" -d '{"source": "mono", "source_item_id": "<id>"}'

# Tell Muse something. kind: likes | dislikes | avoid (needs subject) | context | fact
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/facts" \
     -d '{"statement": "Loves slow, sad Dutch ballads", "kind": "likes", "domain": "track", "subject": "dutch ballad", "confidence": 0.8}'
```

Facts with a sentiment move the taste vector within minutes. A fact can create a taste in a
domain that has no history yet. Facts that name a `subject` act exactly: `avoid` filters, while
likes and dislikes move the score and appear in `why`.

**Questions**: Muse asks where behaviour is ambiguous. Show them in your app, or let an assistant
ask them:

```bash
curl -H "$H" "$MUSE/v1/users/me/questions?limit=3"
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/questions/<id>/answer" -d '{"option": "love"}'   # or {"text": "..."}
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/questions" \
     -d '{"prompt": "Do you listen to music while working?", "kind": "yes_no"}'        # queue your own
```

**Settings and goals**:

```bash
curl -X PUT -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/settings" -d '{"explore_ratio": 0.2, "taste_blend": 0.3}'
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/goals" \
     -d '{"title": "Get into jazz", "domain": "track", "genres": ["jazz"], "target": 20}'
```

Matching items get a slot in recommendations ("For your goal: Get into jazz"). Progress counts
finished items, and a `goal` webhook fires when it changes.

## Right now

```bash
# Mood for three hours: a preset or your own words (embedded in text and sound space)
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/context" \
     -d '{"kind": "mood", "value": {"mood": "rainy sunday, cooking for friends"}, "ttl_minutes": 180}'
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/context" -d '{"kind": "activity", "value": {"activity": "working_out"}}'
curl -X DELETE -H "$H" "$MUSE/v1/users/me/context/mood"
curl -H "$H" "$MUSE/v1/users/me/context"            # active signals and rooms
curl -H "$H" "$MUSE/v1/moods"                       # presets
```

Kinds:
- `mood`
- `activity`: `cooking`, `working`, `studying`, `working_out`, `running`, `sleeping`, `driving`, `commuting`, `party`, `dinner`, `relaxing`, `reading`, `tv`, `gaming`
- `device`
- `presence`
- `location`
- `custom`

Room-level signals go to `/v1/rooms/{room}/context` and household-level ones to
`/v1/household/context` (both admin). Muse also reads:
- the weather (Open-Meteo, `MUSE_WEATHER_LAT/LON`);
- calendars (`POST /v1/users/me/calendars {"url": "<ICS link>"}`). Only the kind of moment is kept (workout, work, holiday...), never titles.

Precedence:
1. explicit request parameters
2. the person's mood
3. activity
4. calendar
5. weather

The recommendation response's `context` shows what was applied and why.

## Groups and Home Assistant

Any number of group sessions can be active at once: A and B in the living room while C and K are
upstairs.

```bash
# Start one (members: user ids or Home Assistant person refs)
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/groups" -d '{"members": ["person.sanne"], "name": "Movie night", "room": "living room"}'
curl -H "$H" "$MUSE/v1/groups/<id>/recommendations?item_type=movie&limit=10"
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/groups/<id>/picks" -d '{"source": "jellyfin", "source_item_id": "<id>"}'
curl -X DELETE -H "$H" "$MUSE/v1/groups/<id>"

# Ad hoc, without a session
curl -H "$H" "$MUSE/v1/group-recommendations?user=me&user=<other id>&item_type=series"
```

How group scoring works:
- **Per member**: each member is scored with their own taste.
- **Group score**: half the weighted average, half the least happy member ("least misery").
- **Vetoes**: anything a member said to avoid, or rated or skipped negatively, is out.
- **Seen** (`seen=all|any|none`, default `all`): only items everyone has seen are dropped, and `seen_by` lists who has.
- **Fairness**: recorded picks remember how well each one fitted each member, and whoever compromised gets more weight next time.

Every item carries `member_fit` and `seen_by`.

**Home Assistant presence**: map people once, then let Home Assistant report rooms. Two or more
people in a room is a session for that room; it is updated and ended as people come and go.

```bash
curl -X PUT -H "Authorization: Bearer $MUSE_ADMIN_TOKEN" -H 'content-type: application/json' \
     "$MUSE/v1/users/<user id>/refs/home_assistant" -d '{"ref": "person.max"}'
curl -X POST -H "Authorization: Bearer $MUSE_ADMIN_TOKEN" -H 'content-type: application/json' \
     "$MUSE/v1/integrations/home-assistant/presence" -d '{"room": "living room", "people": ["person.max", "person.sanne"]}'
```

A Home Assistant `rest_command` plus an automation on presence or occupancy changes is enough
(see [Recipes](#recipes)). **Vesper** can use `GET /group-recommendations?session=<id>` on the
Recommendation API with its read key, or `?user_id=a&user_id=b` for "who's watching" picked in
the app.

## Music beyond the library

```bash
curl -H "$H" "$MUSE/v1/users/me/releases"             # new and upcoming releases by artists you love
curl -H "$H" "$MUSE/v1/wanted?user=me"                # what you'd want acquired
curl -H "$H" "$MUSE/v1/catalog/artists"               # artists the catalog follows, their tags and related artists
curl -H "$H" "$MUSE/v1/users/me/recommendations?item_type=track&source=catalog"   # discoveries not in the library
```

The **wanted list** collects new releases, the best discoveries (daily) and Mono wishlist songs.
Each row has MusicBrainz ids where known (`mbid`, `artist_mbid`), a `reason`, a `score` and a
`status`.

For a music-Radarr:

```bash
# 1. Poll
curl -H "Authorization: Bearer <read-only household or personal token>" "$MUSE/v1/wanted?status=wanted&min_score=0.5"
# 2. Report back (household admin or the person)
curl -X PUT -H "$H" -H 'content-type: application/json' "$MUSE/v1/wanted/<id>" -d '{"status": "grabbed", "external_id": "my-tool-123"}'
```

The `wanted` and `new_release` webhooks push the same data the moment it appears. When the music
shows up in Mono, the row flips to `in_library` by itself.

**Lidarr-style import list**: `GET /v1/wanted/lidarr?token=<read-only token>` returns
`[{"MusicBrainzId", "ArtistMusicBrainzId", "ArtistName", "AlbumMusicBrainzId", "AlbumTitle"}]`.
The token is in the URL because list importers can't send headers, so use a read-only token for
this. Check that your Lidarr version offers a custom/JSON list type before relying on it.

## People and accounts

```bash
curl -H "$H" "$MUSE/v1/me"
curl -X POST -H "$H" "$MUSE/v1/identity/link-codes"                      # on account A: {"code": "K7PM-Q2XD"}, valid 15 minutes
curl -X POST -H "$H_B" -H 'content-type: application/json' "$MUSE/v1/identity/link" -d '{"code": "K7PM-Q2XD"}'   # on account B
curl -X DELETE -H "$H" "$MUSE/v1/identity/aliases/<id>"                  # split off again
curl -H "Authorization: Bearer $MUSE_ADMIN_TOKEN" "$MUSE/v1/users"       # everyone, with linked ids
```

Only link accounts that belong to the same person: linking shares the whole profile.

## Your data

```bash
# Imports: Spotify extended history / liked songs, YouTube Takeout, Goodreads, StoryGraph, SoundCloud (files or a zip)
curl -X POST -H "$H" -F "files=@my_spotify_data.zip" "$MUSE/v1/users/me/imports/spotify"
curl -X POST -H "$H" -F "files=@watch-history.json" "$MUSE/v1/users/me/imports/youtube"
curl -X POST -H "$H" -F "files=@goodreads_library_export.csv" "$MUSE/v1/users/me/imports/goodreads"

curl -H "$H" "$MUSE/v1/users/me/export" > muse-export.json                                     # everything
curl -X DELETE -H "$H" "$MUSE/v1/users/me/events?since=2026-11-14&until=2026-11-17&confirm=true" # forget a weekend
curl -X DELETE -H "$H" "$MUSE/v1/users/me?confirm=true"                                         # erase the person entirely
curl -X PUT -H "$H" -H 'content-type: application/json' "$MUSE/v1/users/me/incognito" -d '{"minutes": 120}'
curl -H "$H" "$MUSE/v1/users/me/access-log"                                                    # who read or changed your data
```

Re-importing the same export adds nothing. Songs you know from an import aren't recommended as
discoveries. Admins set per-source retention with `PUT /v1/retention {"searxng": 90}`.

## Webhooks

```bash
curl -X POST -H "$H" -H 'content-type: application/json' "$MUSE/v1/webhooks" \
     -d '{"url": "https://example.org/hooks/muse", "events": ["new_release", "wanted", "digest"]}'
# {"id": ..., "secret": "whsec_...", ...}   (shown once)
curl -X POST -H "$H" "$MUSE/v1/webhooks/<id>/test"
```

| Event | When | Data |
|---|---|---|
| `new_release` | an artist you love has a new (or announced) release | artist, title, mbids, type, date, upcoming |
| `wanted` | something joined the wanted list | id, kind, artist, title, mbids, origin, reason, score |
| `digest` | weekly (`MUSE_DIGEST_WEEKDAY`, default Monday) | picks per domain, new releases, a question, goals |
| `goal` | goal progress changed or a goal was met | id, title, progress, target, done |
| `group` | a group session started | session |
| `context` | a context signal was set | kind, room, value |
| `test` | `POST /v1/webhooks/{id}/test` | |
| `*` | all of the above | |

Delivery is a `POST` with this JSON body: `{"id", "event", "data": {..., "user_id"}, "sent_at"}`.
Headers: `X-Muse-Event`, `X-Muse-Delivery` and `X-Muse-Signature: sha256=<hex>`.

Verify the signature over the raw body:

```python
import hashlib, hmac
ok = hmac.compare_digest(request.headers["X-Muse-Signature"], "sha256=" + hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest())
```

Failed deliveries are retried after 1, 5, 30, 120 and 720 minutes. Respond 2xx quickly.
Personal hooks only get events about their person; household hooks (admin, `"user": null`) get
everything. URLs that resolve to private addresses are refused for non-admins (see
[`../security-audit.md`](../security-audit.md)).

## Sending events

Sources post to the **Ingestion API** with their own key (`MUSE_ADAPTER_KEYS="mono:<key>,..."`).
The key decides the source; the body's `source` is overridden:

```bash
curl -X POST http://127.0.0.1:8771/events -H 'X-Muse-Api-Key: <key>' -H 'content-type: application/json' -d '{
  "user_id": "<uuid>", "source": "mono", "source_item_id": "<track id>", "item_type": "track", "action": "completed",
  "progress_pct": 100, "timestamp": "2026-11-20T21:04:00Z",
  "metadata": {"title": "Racoon - Oceaan", "artist": "Racoon", "album": "Liverpool Rain", "genres": ["dutch rock"], "dedupe_key": "listen:123"},
  "device_context": {"device": "Pixel 9", "tz": "Europe/Amsterdam"}
}'
```

Guidelines:
- Send `cataloged` once per item with rich metadata, so Muse can recommend it before anyone plays it.
- Put `metadata.dedupe_key` on everything you might re-send.
- Add `device_context` so context tastes can form.

Actions and default weights: `shared/muse_schema/event.py`.

## Recipes

**A player's "Recommended for you" row** (Mono does exactly this):
`GET /recommendations/{user}?item_type=track&source=mono&tz=...` on the Recommendation API with
a read key. Show `why[0]` under each item.

**Autoplay that flows**: when two songs are left in the queue, call
`GET /next/{user}?source=mono&source_item_id=<current>&history=mono:<id>...&playable_source=mono`
and append the results.

**Home Assistant**:

```yaml
rest_command:
  muse_presence:
    url: "http://muse:8772/v1/integrations/home-assistant/presence"
    method: POST
    headers: { Authorization: !secret muse_admin_bearer }
    content_type: "application/json"
    payload: '{"room": "{{ room }}", "people": {{ people | to_json }}}'
  muse_activity:
    url: "http://muse:8772/v1/rooms/{{ room }}/context"
    method: POST
    headers: { Authorization: !secret muse_admin_bearer }
    content_type: "application/json"
    payload: '{"kind": "activity", "value": {"activity": "{{ activity }}"}, "ttl_minutes": 90}'

automation:
  - alias: Muse living room presence
    trigger: [{ platform: state, entity_id: [binary_sensor.living_room_max, binary_sensor.living_room_sanne] }]
    action:
      - service: rest_command.muse_presence
        data:
          room: living room
          people: >
            {{ [ 'person.max' if is_state('binary_sensor.living_room_max', 'on'),
                 'person.sanne' if is_state('binary_sensor.living_room_sanne', 'on') ] | select | list }}
```

**Vesper "who's watching?"**: let the person pick profiles, then call
`GET /group-recommendations?user_id=<a>&user_id=<b>&item_type=movie` (Recommendation API, read
key). When they press play, `POST /v1/groups/{id}/picks` (Query API) if you started a session.

**Open WebUI / any LLM app**: add the Query API as an OpenAPI tool server
(`http://<host>:8772`, personal bearer token), or the MCP endpoint. See
[`../adapters/openwebui.md`](../adapters/openwebui.md).

**A Python client**:

```python
import httpx
muse = httpx.Client(base_url="http://127.0.0.1:8772", headers={"Authorization": "Bearer muse_..."})
for r in muse.get("/v1/users/me/recommendations", params={"item_type": "movie", "mood": "cozy", "limit": 5}).json()["recommendations"]:
    print(r["title"], "-", r["why"][0] if r["why"] else "")
```

**JavaScript**:

```js
const muse = (path, init = {}) => fetch(`http://127.0.0.1:8772${path}`, { ...init, headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json', ...init.headers } }).then((r) => r.json());
const { tracks } = await muse('/v1/users/me/radio?length=20&arc=build&playable_source=mono');
```

## Errors, limits, versioning

- **Errors**: JSON `{"detail": "..."}`. Statuses:
  - `400` bad input
  - `401` no or invalid token
  - `403` not allowed (someone else's data, read-only token, not in that group)
  - `404` unknown
  - `409` already answered
  - `422` validation
  - `429` too many attempts (link codes)
  - `503` a dependency (the recommendation API or audio analyzer) is unavailable
- **Limits**: list endpoints cap `limit` (usually 100). Imports are capped at 300 MB per file and 1 GB unpacked. The ingestion API rate-limits per source.
- **Versioning**: paths are under `/v1`. Additive changes (new fields, new endpoints) don't bump the version; anything breaking would ship as `/v2` alongside.
