# Jellyfin adapter setup

The adapter (`adapters/jellyfin/`) polls your existing Jellyfin server directly — no plugin
required on the Jellyfin side for this part. It runs two jobs on independent intervals: a slow
full-library catalog sync, and a fast per-user watch-state sync.

This is what lets Muse start producing recommendations against a library that already exists,
before any new watch history accumulates: the catalog sync alone gives the embedding worker
enough to build content embeddings for everything you already have.

## 1. Create a Jellyfin API key

In Jellyfin admin: **Dashboard → API Keys → +** (New API Key). Name it `muse-adapter`. Copy the key.

## 2. Configure

In `.env`:
```
JELLYFIN_URL=http://jellyfin:8096          # your existing Jellyfin container/host
JELLYFIN_API_KEY=<the key from step 1>
MUSE_JELLYFIN_ADAPTER_KEY=<the jellyfin key from MUSE_ADAPTER_KEYS>
```

Optional tuning (defaults are fine to start):
```
MUSE_CATALOG_SYNC_INTERVAL_SECONDS=86400   # full library rescan interval
MUSE_WATCH_POLL_INTERVAL_SECONDS=300       # per-user watch-state poll interval
```

## 3. Start it

```bash
docker compose up -d jellyfin-adapter
```

On first startup it immediately runs a full catalog sync — for a large library this can take a
while and generates one event per item, so expect a burst of ingestion traffic the first time.
Watch the logs:
```bash
docker compose logs -f jellyfin-adapter
```

## 4. Verify

```bash
docker compose exec postgres psql -U muse -d muse -c \
  "SELECT action, count(*) FROM event WHERE source='jellyfin' GROUP BY action;"
```
You should see a large `cataloged` count matching your library size, and (once the embedding
worker's next cycle runs) content embeddings appearing in Qdrant's `content_items` collection.

Play something in Jellyfin, wait for the next watch-poll cycle, then check:
```bash
docker compose exec postgres psql -U muse -d muse -c \
  "SELECT action, progress_pct, metadata->>'title' FROM event WHERE source='jellyfin' AND action != 'cataloged' ORDER BY timestamp DESC LIMIT 5;"
```

## Notes / known limitations

- **User identity**: this adapter uses Jellyfin's own per-user GUID directly as the Muse
  `user_id` — Jellyfin users are auto-imported as Muse profiles with no extra mapping step. Other
  adapters' identity resolution (matching *their* usernames to this same `user_id`) is described
  in `docs/architecture.md` but not yet implemented for any adapter besides Jellyfin.
- **Polling, not push**: this v1 adapter polls rather than reacting to live playback events. That's
  good enough for watch-history/completion/favorite signal, but not for the real-time "Soon Gone"
  whitelist-by-watching mechanism (docs/architecture.md), which needs a playback-*start* hook — that
  lands with the Jellyfin plugin (`jellyfin-plugin/`), not this adapter.
- **In-memory state, no local DB**: adapters are write-only by design (see Security model in
  `docs/architecture.md`) — this adapter keeps zero local persistent state. A restart re-diffs
  watch-state from scratch and may emit a small number of duplicate `completed`/`favorited`
  events; harmless in practice (a duplicate `completed` just nudges that item's taste-weight
  slightly, it doesn't corrupt anything).
- Only `Movie`, `Episode`, `Audio`, `AudioBook`, and `Book` library item types are captured;
  `Series`/`Season`/`BoxSet`/`Playlist` container types are skipped (their child items carry the
  signal already).
