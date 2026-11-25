# Audiobookshelf setup

Status: implemented (`adapters/audiobookshelf`, polls the Audiobookshelf API).

What it captures: every book (`cataloged`, with author, narrator, series, genres and
description, so recommendations work on content), and per user `played` (once a day while
listening) and `completed` (when finished). Podcasts are not handled yet.

Setup: `.env` gets `AUDIOBOOKSHELF_URL=http://audiobookshelf:80`; put an admin user's API token
(Settings -> Users -> admin -> API token) in `secrets/audiobookshelf_token`. The ingestion key
is generated. Start with `docker compose --profile audiobookshelf up -d audiobookshelf-adapter`.

Users appear as `uuid5("audiobookshelf:<user id>")`; link them with link codes.
Verify: `SELECT action, count(*) FROM event WHERE source = 'audiobookshelf' GROUP BY 1;`
