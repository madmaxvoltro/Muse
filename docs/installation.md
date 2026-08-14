# Installation

This assumes you already run Jellyfin, the arr-stack, SearxNG, Open WebUI, Navidrome, and/or
Audiobookshelf somewhere reachable from the host you're installing Muse on. Muse only adds a
personalization layer on top — it does not replace any of these.

## 0. Network isolation (do this first)

Muse's event store aggregates a behavioral profile across nearly all your personal data — treat
it as your highest-value target. Before installing anything:

1. Put the Muse host (a dedicated VM/LXC is strongly recommended) on its own VLAN segment,
   separate from the rest of your homelab.
2. Make sure the host is reachable only via Tailscale (or your own VPN/reverse-proxy setup) —
   never expose Muse's ports directly to your LAN or the internet.
3. If you use LUKS for disk encryption elsewhere, extend it to this host's data volume too.

## 1. Prerequisites

- Docker + Docker Compose v2
- A dedicated VM/LXC (recommended: 4 vCPU / 8GB RAM minimum to start; more once the embedding
  worker is running local models)
- `openssl` (for generating adapter API keys)

## 2. Clone and configure

```bash
git clone <this-repo-url> muse
cd muse
cp .env.example .env
```

Edit `.env`:

- `MUSE_POSTGRES_PASSWORD` — generate with `openssl rand -hex 32`
- `MUSE_ADAPTER_KEYS` — one key per adapter you plan to run. Generate each separately:
  ```bash
  openssl rand -hex 32   # repeat per adapter, never reuse a key across adapters
  ```
- `MUSE_DEFAULT_USER_ID` — a UUID for yourself until multi-user identity resolution is wired up:
  ```bash
  python3 -c "import uuid; print(uuid.uuid4())"
  ```
- Adapter-specific upstream URLs (e.g. `SEARXNG_UPSTREAM_URL`) — point these at your existing
  services.

## 3. Start the core stack

```bash
docker compose up -d postgres qdrant ingestion-api recommendation-api
```

The Postgres container runs `db/migrations/*.sql` automatically on first boot (via
`docker-entrypoint-initdb.d`). Verify:

```bash
curl http://localhost:8000/healthz   # ingestion-api, if you've exposed it locally for testing
```

In production, don't expose these ports on the host at all — reach them only over the
Tailscale/VLAN network from the adapter containers, which is how `docker-compose.yml` is wired
by default (services talk to each other by container name, not published ports).

## 4. Bring adapters online one at a time

Each adapter has its own setup tutorial, since each upstream service is wired in differently:

- [SearxNG](adapters/searxng.md) — implemented, reference adapter (proxy-style)
- [Jellyfin](adapters/jellyfin.md) — implemented (poll-style); start with this one, it backfills
  your existing library so recommendations have something to work with from day one
- [Open WebUI](adapters/openwebui.md) — not yet implemented
- [Navidrome](adapters/navidrome.md) — not yet implemented
- [Audiobookshelf](adapters/audiobookshelf.md) — not yet implemented
- [arr-stack](adapters/arr.md) — not yet implemented

Bring adapters online one at a time and check `docker compose logs -f <adapter>` after each —
easier to spot a misconfigured upstream URL or bad API key than debugging all of them at once.

## 5. Jellyfin plugin

Not yet implemented — see `jellyfin-plugin/README.md`. Once built, install like any other
Jellyfin plugin (drop the compiled `.dll` into Jellyfin's plugin directory, or add the Muse
plugin repository URL in Jellyfin's admin dashboard under Plugins → Repositories).

## 6. Verify end-to-end

1. Trigger an action in a connected source (e.g. run a SearxNG search).
2. Check it landed in the event store:
   ```bash
   docker compose exec postgres psql -U muse -d muse -c "SELECT source, action, metadata FROM event ORDER BY timestamp DESC LIMIT 5;"
   ```
3. Once the embedding worker and recommendation API are implemented, check
   `GET /recommendations/<your-user-id>` on the recommendation-api returns something.

## Troubleshooting

- **Adapter can't reach ingestion-api**: check it's on the `muse-net` Docker network and using
  the container name (`http://ingestion-api:8000`), not `localhost`.
- **401 from ingestion-api**: the adapter's `X-Muse-Api-Key` doesn't match an entry in
  `MUSE_ADAPTER_KEYS` — check both are set from the same `.env`.
- **429 from ingestion-api**: the per-source rate limit tripped (300 events/60s by default,
  see `services/ingestion-api/app/ratelimit.py`) — likely a misbehaving adapter in a retry loop,
  check its logs before raising the limit.
