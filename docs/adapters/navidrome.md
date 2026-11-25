# Navidrome setup

Status: implemented (`adapters/navidrome`, polls the Subsonic API).

What it captures:

| Navidrome | Muse |
|---|---|
| every song (daily) | `cataloged` (artist, album, year, genres, duration) |
| a song appears in "now playing" | `played` (device = player name) |
| it gets replaced after >= 80% / the last 30 s | `completed` |
| it gets replaced before 50% | `skipped` |
| starred / unstarred (per-user credentials) | `favorited` / `unfavorited` |

Setup: `.env` gets `NAVIDROME_URL=http://navidrome:4533` and `NAVIDROME_USER=<admin user>`
(sees everyone's "now playing"); secrets go in `secrets/` (created empty by `./muse up`):

- `secrets/navidrome_password`: that user's password
- `secrets/navidrome_users`: optional, one `user:password` per line, for each person's stars

The ingestion key (`secrets/adapter_key_navidrome`) is generated. Start with
`docker compose --profile navidrome up -d navidrome-adapter`.

Users appear in Muse as `uuid5("navidrome:<username>")`. Link each to the person's other
accounts (a link code from Mono or an assistant) so it's one profile.

Verify: `SELECT action, count(*) FROM event WHERE source = 'navidrome' GROUP BY 1;`
