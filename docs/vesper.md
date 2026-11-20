# Muse for clients: Vesper, Mono and others

This is the contract between Muse and the apps that use it as their taste engine. Vesper
(video) and Mono (music) are the two first-party clients; the Jellyfin plugin uses a subset.

## Identity

- Every app sends events and asks for recommendations with **its own** `user_id` (a UUID).
  Vesper uses the profile's Jellyfin user GUID (so Vesper events and the Jellyfin adapter's
  events land on the same person); Mono uses each account's `muse_user_id`.
- **Linking** joins identities of one person across apps (migration 007, `app/identity.py`):
  1. App A: `POST /identities/{user_id}/link-code {source, label}` → `{code: "4KX-9TQ", expires_at}` (15 min).
  2. App B: `POST /identities/link {code, user_id, source, label}` (codes are case- and dash-insensitive).
  3. App A's identity is now canonical, B's an alias. B's history moves over (kept tagged with
     `metadata.original_user_id`), new B events are written onto A by the ingestion API, and every
     read endpoint resolves B → A. Both apps keep using their own ids.
  4. `GET /identities/{user_id}` lists links (works with either id); `DELETE /identities/{user_id}/links/{alias}`
     unlinks and moves the alias' events back.
- `DELETE /users/{user_id}` wipes a person (events, overlay, vectors, curator log). Linked
  aliases are released with their own events, not wiped.

## Keys

| Key | Env | Can |
|---|---|---|
| Ingestion | `MUSE_ADAPTER_KEYS=source:key,…` | `POST /events`, `POST /events/batch` (the key fixes `source`) |
| Read | `MUSE_READ_KEYS=name:key,…` | every `GET`, `POST /search/semantic` |
| Control | `MUSE_CONTROL_KEYS=name:key,…` | also overlay/mood, Soon Gone keep, linking, `/watching`, user wipe |

Headers: `X-Muse-Api-Key` (ingestion), `X-Muse-Read-Key` (read and control keys). With no
control keys configured, read keys may also write (backwards compatible).

## Events to send

The more a client tells Muse, the better it understands taste. Vesper sends all of these:

| Action | When | Notes |
|---|---|---|
| `played` | playback stopped / every few minutes | `progress_pct`, `duration_ms` (session length), `metadata.watched_ms` |
| `completed` | credits reached or ≥ 90 % | counts once per item per day, whichever source reports it |
| `skipped` | abandoned early (< 15 % and < 20 min watched) | a real negative signal |
| `rated` | thumbs | `metadata.value` −1/0/1/2, `action_weight` from `RATED_WEIGHTS`; only the latest counts |
| `favorited` / `added_watchlist` / `removed` | My List on / added / off | |
| `detail_viewed` | detail page opened | `metadata.from`: `row:<row id>`, `search`, `more_like_this:<id>`, `surprise` |
| `trailer_played` | trailer started | |
| `searched` | query the person stopped typing at | `metadata.query` |
| `impression` | shown in a Muse row | weight 0; feeds fatigue (shown often, never picked). Use `/events/batch` |
| `requested` | asked for a title to be added | protects it from the curator |

Context on any event: `device_context` (`{"client": "tv" | "web" | "android", "hour": 21, "weekday": 4}`),
`metadata.together` (other user_ids in the room) and `metadata.co_viewer: true` on the copies
sent for the people who didn't hold the remote (counted at 0.6×).

Item metadata (merged into the item's vector payload, never erased by thinner events): `title`,
`description`, `genres`, `year`, `directors`, `cast`, `studios`, `tagline`, `official_rating`,
`runtime_minutes`, `community_rating`, `added_at`, `tmdb_id`/`tvdb_id`/`imdb_id`, and for episodes
`series_id`, `series_name`, `season`, `episode`.

## Asking

All under the recommendation API. `user_id` may be an alias.

| Endpoint | What |
|---|---|
| `GET /recommendations/{user_id}?item_type=movie,series&row=…` | A row. `row`: `picks`, `because_you_watched` (Muse picks the anchor; `seed_rank=0,1`), `more_like_this&seed=<id>`, `new_arrivals`, `rewatch`. Options: `limit`, `explore_ratio`, `max_age`, `allow_unrated`, `exclude=id,id`, `mood`, `with_users=<ids>` (group) |
| `GET /recommendations/group?user_ids=a,b` | For everyone in the room (average + least misery, leans on what the group watched together) |
| `GET /explain/{user_id}/{item_id}` | "Why this" for any title: `match` (%), `reason_codes` |
| `POST /search/semantic {query, user_id?, item_types, max_age}` | Search by meaning, nudged toward the person's taste |
| `GET/PUT /users/{user_id}/overlay` | Explore dial (`explore_ratio`) and mood (`mood`, `mood_hours`, `clear_mood`) |
| `GET /moods` | `light`, `edge`, `short`, `mindbending`, `comfort`, `cozy`, `epic` |
| `POST /watching {user_ids}` | Who is watching together right now (remembered as a group) |
| `GET /prefetch/{user_id}?horizon_hours=24&tz=Europe/Amsterdam` | What they'll likely start soon (`in_progress`, `series_next`, `recommended`) with likelihoods |
| `GET /wrapped/{user_id}?year=&tz=&kids=` | The year in film: minutes, top films/series, genres, hours, streaks |
| `GET /soon-gone`, `POST /soon-gone/{id}/whitelist?user_id=` | Curator removals with Jellyfin ids; Keep accepts tmdb/tvdb or Jellyfin ids |
| `GET /stats` | Event volume per source/action, users, embedding progress, model reachability |

Every recommendation carries `source`, `source_item_id`, `item_type`, `title`, `score`,
`confidence`, `match` (1–99), `explore`, `reason_codes` and the numeric `explanation`. Episodes are
rolled up to their series when `series` is asked for.

### Reason codes

`because_loved | because_favorited | because_liked | because_finished | because_watched`
(`anchor_id`, `anchor_title`), `same_director`, `same_cast` (`value` + anchor), `watched_together`,
`genre_affinity`, `searched_for` (`value` = query), `mood`, `highly_rated`, `new_arrival`,
`explore_pick`. Clients word and translate them; at most four per item, strongest first.

## How taste is modelled

- **Content vectors**: a local sentence model (`all-MiniLM-L6-v2`) over title, tagline, overview,
  genres and "Directed by… / Starring… / From…", so people and studios shape similarity.
- **History per item** (`app/history.py`): finished, rewatched, loved, liked, favourited, abandoned,
  rejected, shown-but-ignored, co-viewed. State actions count once; sources never double count.
- **Taste facets** (`app/taste.py`): liked titles are clustered (weighted spherical k-means, up to 4)
  so "slow science fiction" and "romantic comedies" each keep their own pull instead of averaging
  into neither. Candidates are searched per facet.
- **Scoring** (`app/funnel.py`): best facet affinity (groups: average + least misery), seed similarity
  for seeded rows, genre features, a community-rating prior, penalties for resembling a thumbs-down
  (very close ones are dropped) and for being shown again and again without a click, a mood term.
- **Re-ranking**: maximal marginal relevance for variety, a genre cap, and explore picks spread
  through the row (the explore dial sets how many).
