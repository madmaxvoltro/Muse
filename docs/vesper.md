# Vesper and Muse

Vesper (madmaxvoltro/vesper) is a Jellyfin client. It tells Muse what happens on screen in far
more detail than the Jellyfin adapter can see by polling, and it asks Muse what to show. This
page is the contract between the two.

| What | Muse service | Endpoint | Key |
|---|---|---|---|
| Events (play, progress, impressions, ratings, co-viewing) | ingestion API | `POST /events`, `POST /events/batch` | `X-Muse-Api-Key` (the `jellyfin` adapter key) |
| Answers (onboarding, this-or-that, abandon reasons, reason corrections) | ingestion API | `POST /answers` | answer key |
| Recommendations, home rows, why this, Wrapped, prefetch, questions | recommendation API | `GET /recommendations/{u}`, `/rows/{u}`, `/why/{u}/{source}/{id}`, `/wrapped/{u}`, `/prefetch/{u}`, `/group-recommendations`, `/questions/{u}/*` | `X-Muse-Read-Key` (`vesper:` line in read_keys) |
| Search by meaning, settings (explore dial), moods | query API | `GET /v1/search`, `GET`/`PUT /v1/users/me/settings`, `GET /v1/moods`, `POST /v1/users/me/context` | adapter key + `X-Muse-User` |
| Link a Vesper profile with a Mono or Muse profile | query API | `POST /v1/identity/link-codes`, `POST /v1/identity/link` | adapter key + `X-Muse-User` |
| Who is watching together | query API | `POST /v1/groups`, `GET /v1/groups/{id}/recommendations`, `POST /v1/groups/{id}/picks`, `DELETE /v1/groups/{id}` | adapter key + `X-Muse-User` |
| Soon gone | recommendation API | `GET /soon-gone`, `POST /soon-gone/{tmdb id}/whitelist` | read key |

## Events

Vesper sends video events as source `jellyfin`, so they join the adapter's events on the same
Jellyfin item ids (`source = "vesper"` is reserved and not used for video). `/events/batch`
takes a JSON list or `{"events": [...]}`; at most 1000 per call, and the `jellyfin` source has a
budget of 1200 events a minute (`MUSE_RATE_LIMIT_PER_MINUTE` raises the default for others).

Metadata Muse understands for films and episodes (all optional, the richer the better): `title`,
`description`, `genres`, `tags`, `year`, `people` (Jellyfin's list of `{name, type}`, with type
Director, Writer, Actor), `studios`, `taglines`, `official_rating`, `runtime_min`,
`community_rating`, `provider_ids` (`{"Tmdb": ..., "Imdb": ..., "Tvdb": ...}`), `series_id`,
`series_name`, `season`, `episode`, `added_at`. The taste engine turns them into one canonical
title per film (a Jellyfin copy and a Radarr entry of the same film are one item) and enriches
it from TMDB.

Signals beyond play and complete:

* `impression` (weight 0): a title was shown in a row and not picked. Used as fatigue, never as
  taste.
* `rated` with `metadata.value` -1 / 1 / 2 (thumbs down, up, love), `favorited` and
  `unfavorited`: state, only the latest one per title counts.
* `abandon_reason`, `chose` and `passed_over` go through `POST /answers` and say *why* someone
  stopped or picked.
* `metadata.co_viewer: true`: this person was in the room but someone else chose. Counts at
  0.6.
* Duplicate reports are collapsed: one completion per film per day, however many sources report
  it.

## Profiles: one person, many apps

A person can have a Vesper profile, a Mono account and an assistant token. Linking them makes
one taste out of all of it:

1. In the app the person already uses (say Mono), `POST /v1/identity/link-codes` gives a short
   code, valid for 15 minutes.
2. In Vesper, `POST /v1/identity/link {"code": "..."}` with `X-Muse-User: <the Vesper profile's
   Muse id>`. From then on both ids are one person for every recommendation, profile and group.
3. `GET /v1/me` shows the linked ids; `DELETE /v1/identity/aliases/{id}` undoes a link.

## Watching together

When Vesper's "who's watching?" has more than one profile, it starts a session:
`POST /v1/groups {"members": [<uuid>, <uuid>], "room": "Living room", "device": "Vesper TV"}`.
Recommendations for the session (`GET /v1/groups/{id}/recommendations?item_type=movie`) are
fair to every member (least misery, vetoes for what someone strongly dislikes, a `member_fit` per
person). What the group picks (`POST /v1/groups/{id}/picks`) counts for everyone in it. Sessions
expire after their `ttl_minutes` or end with `DELETE`. Home Assistant presence can create
sessions per room on its own.

## Rows and why this

`/rows/{u}?item_type=movie` returns a home screen of rows whose titles are their reasons (Top
picks, Because you watched X, More of an interest, Watch again, From your watchlist, New in
your library, Something different). A "Because you watched" row carries `seed` with the
title's `source` and `source_item_id`, so Vesper shows its own artwork. Every item has
`reasons` (readable text) and `reason_details` (type, id and the corrections a client may offer
through `POST /answers` kind `reason`). `/why/{u}/jellyfin/{id}` gives the same for one title.

## Wrapped

`/wrapped/{u}?year=2026&tz=Europe/Amsterdam` is a person's year in film: minutes (from runtime
and furthest progress, once per title per day), films, episodes, binge days, longest streak,
top films and series, genres, when they watch. `kids=true` leaves out the time statistics.

## Kids profiles

`/recommendations/{u}`, `/rows/{u}` and the query API's `/v1/search` take `max_age` (0-21): only titles whose official rating
(US, NL Kijkwijzer, DE FSK, UK BBFC, FR, AU) allows that age. Unrated titles are left out unless
`allow_unrated=true`.

## Prefetch

`GET /prefetch/{u}?horizon_hours=24&tz=Europe/Amsterdam` lists what the person will probably
start in the next hours: films in progress, the next episode of series they follow, a few top
picks, each with a likelihood from their own weekday and hour pattern. Vesper moves these to
fast storage and offers them as smart downloads on phones.
