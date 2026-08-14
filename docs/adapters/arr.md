# arr-stack adapter setup

The adapter (`adapters/arr/`) polls Radarr and/or Sonarr directly — either can be left
unconfigured (blank `RADARR_URL`/`SONARR_URL`) if you only run one of them.

This is also what the curator (`services/curator/`) needs configured — it's the same
Radarr/Sonarr instance, just a different piece of code talking to it (the curator adds/removes
content; this adapter only reads, to learn from what gets added/rejected).

## 1. Get API keys

Radarr: **Settings → General → Security → API Key**. Sonarr: same path. Copy both (or
whichever you run).

## 2. Configure

In `.env`:
```
RADARR_URL=http://radarr:7878
RADARR_API_KEY=<from step 1>
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=<from step 1>
MUSE_ARR_ADAPTER_KEY=<the arr key from MUSE_ADAPTER_KEYS>
```

If you want the curator to use a specific quality profile / root folder instead of
whatever's first in your Radarr/Sonarr config, also set `RADARR_QUALITY_PROFILE_NAME`,
`RADARR_ROOT_FOLDER`, `SONARR_QUALITY_PROFILE_NAME`, `SONARR_ROOT_FOLDER` (see `.env.example`).

## 3. Start it

```bash
docker compose up -d arr-adapter
```

First run backfills your entire current Radarr/Sonarr library as `added_watchlist` events —
expect a burst of ingestion traffic once, same as the Jellyfin adapter's first catalog sync.

## 4. Verify

```bash
docker compose exec postgres psql -U muse -d muse -c \
  "SELECT action, count(*) FROM event WHERE source='arr' GROUP BY action;"
```

## Notes

- **Series are tracked at the whole-series level**, not per-episode — the Muse schema's
  `series_episode` item type is used as an approximation since Radarr/Sonarr's own concept of
  "added to library" is series-level in Sonarr, while actual watch/completion signal (which
  genuinely is per-episode) comes from the Jellyfin adapter instead.
- This adapter can't tell whether a removal was curator-driven or a human manually deleting
  something in Radarr/Sonarr's UI — see `services/curator/README.md` for why that's an accepted
  v1 tradeoff, not a bug.
- Also required for the curator's add/remove calls (`services/curator/arr_client.py`) to work —
  without `RADARR_API_KEY`/`SONARR_API_KEY` set, the curator's add cycle will fail on every
  candidate and log the failure, but won't crash.
