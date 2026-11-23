# Navidrome adapter

Status: implemented (polling the Subsonic API). Setup: [`docs/adapters/navidrome.md`](../../docs/adapters/navidrome.md).

Emits `cataloged` (whole library, daily), `played` / `completed` / `skipped` from getNowPlaying
(how long a song ran decides), `favorited` / `unfavorited` from stars (needs per-user
credentials in `NAVIDROME_USERS`). Users are `uuid5("navidrome:<username>")`; link them to the
person's other accounts with a link code.
