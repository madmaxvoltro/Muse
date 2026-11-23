# Audiobookshelf setup

Status: implemented (`adapters/audiobookshelf`, polls the Audiobookshelf API).

What it captures: every book (`cataloged`, with author, narrator, series, genres and
description, so recommendations work on content), and per user `played` (once a day while
listening) and `completed` (when finished). Podcasts are not handled yet.

`.env`:

```bash
MUSE_ADAPTER_KEYS=...,audiobookshelf:<key>
MUSE_AUDIOBOOKSHELF_ADAPTER_KEY=<key>
AUDIOBOOKSHELF_URL=http://audiobookshelf:80
AUDIOBOOKSHELF_TOKEN=<admin user's API token: Settings -> Users -> (admin) -> API token>
```

Start: `docker compose --profile audiobookshelf up -d audiobookshelf-adapter`.

Users appear as `uuid5("audiobookshelf:<user id>")`; link them with link codes.
Verify: `SELECT action, count(*) FROM event WHERE source = 'audiobookshelf' GROUP BY 1;`
