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

`.env`:

```bash
MUSE_ADAPTER_KEYS=...,navidrome:<key>
MUSE_NAVIDROME_ADAPTER_KEY=<key>
NAVIDROME_URL=http://navidrome:4533
NAVIDROME_USER=admin            # sees everyone's "now playing"
NAVIDROME_PASSWORD=...
NAVIDROME_USERS=alice:pw,bob:pw # optional, for each person's stars
```

Start: `docker compose --profile navidrome up -d navidrome-adapter`. The audio analyzer uses the
same credentials to listen to Navidrome songs.

Users appear in Muse as `uuid5("navidrome:<username>")`. Link each to the person's other
accounts (a link code from Mono or an assistant) so it's one profile.

Verify: `SELECT action, count(*) FROM event WHERE source = 'navidrome' GROUP BY 1;`
