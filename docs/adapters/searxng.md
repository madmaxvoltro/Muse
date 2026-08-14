# SearxNG adapter setup

The adapter (`adapters/searxng/`) is a thin reverse-proxy sidecar: `nginx -> muse-searxng-adapter
-> SearxNG`. It logs every `/search` request as an event then transparently forwards it — SearxNG
itself is never modified, so it survives SearxNG updates untouched.

## 1. Point the adapter at your SearxNG instance

In `.env`:
```
SEARXNG_UPSTREAM_URL=http://searxng:8080   # your existing SearxNG container/host
MUSE_SEARXNG_ADAPTER_KEY=<the searxng key from MUSE_ADAPTER_KEYS>
```

## 2. Start it

```bash
docker compose up -d searxng-adapter
```

## 3. Re-point nginx at the adapter instead of SearxNG directly

Wherever your reverse proxy currently forwards to SearxNG, change the upstream to the adapter
container instead:

```nginx
location / {
    proxy_pass http://muse-searxng-adapter:8080;   # was: http://searxng:8080
    proxy_set_header Host $host;
}
```

Reload nginx. SearxNG's own network exposure can now be restricted to only accept connections
from the adapter, not directly from nginx.

## 4. Verify

Run a search through your normal SearxNG URL, then check:
```bash
docker compose exec postgres psql -U muse -d muse -c \
  "SELECT metadata FROM event WHERE source='searxng' ORDER BY timestamp DESC LIMIT 1;"
```
You should see your query text in `metadata`.

## Notes

- The adapter currently attributes all searches to `MUSE_DEFAULT_USER_ID` — per-user attribution
  needs identity resolution wired up first if multiple people share this SearxNG instance (see
  `docs/architecture.md` — Multi-user section).
- If ingestion fails for any reason, the search itself still succeeds — the adapter never blocks
  the proxied request on a failed log write.
