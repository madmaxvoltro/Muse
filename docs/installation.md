# Installing Muse

Muse is a backend: it learns from Jellyfin (and whatever else you connect) and serves
recommendations to clients such as Vesper and the Jellyfin plugin. Setup is one command.

## Before you start

* A Linux host (VM, LXC or bare metal) with **Docker and Docker Compose v2**, and Python 3.9+
  (only for the `./muse` tool; no packages needed).
* 8 GB RAM minimum, 16 GB+ for the default embedding model on CPU, or an NVIDIA GPU. 15 GB disk.
* A **Jellyfin API key**: Jellyfin Dashboard > API Keys > +.
* Optional but recommended: a free **TMDB API key** (https://www.themoviedb.org/settings/api).
  It adds keywords, cast and "fans also liked" to every title. TMDB only sees which titles are
  looked up, never who watched what.

Muse holds a behavioural profile of your household. Give it its own VM/LXC if you can, keep it
off the public internet, and read [SECURITY.md](../SECURITY.md).

## Install

```bash
git clone <this repository> muse && cd muse
./muse setup
```

The setup asks a few questions (Enter accepts the default) and then does everything:

1. checks Docker, memory, disk and GPU;
2. asks how Muse will be reached:
   * **local**: Jellyfin and Vesper run on this machine. Most private: Muse only listens on
     localhost.
   * **lan**: other machines on your network connect, over HTTPS with Muse's own certificate.
   * **tailscale**: other machines connect over Tailscale, over HTTPS on your tailnet address;
3. checks your Jellyfin URL and key (and shows your server name and number of users);
4. checks your TMDB key;
5. picks an embedding model for your hardware, and optionally an LLM (local Ollama/vLLM, or an
   external one only if you explicitly allow it);
6. generates every secret into `secrets/` (encryption key, database passwords, API keys, client
   keys). Re-running setup never replaces existing secrets;
7. builds and starts Muse, applies database migrations, and verifies it's up.

At the end it prints what your clients need: the API addresses, the Vesper read and answer keys,
the Jellyfin plugin key, and a link to the status page. `./muse keys` shows them again.

**Back up `secrets/muse_master_key`** somewhere safe and separate (a password manager, a USB
stick). Everything personal Muse stores is encrypted with it. Without it nobody can read the
data, including you.

Non-interactive, for automation:

```bash
./muse setup --mode local --jellyfin-url http://127.0.0.1:8096 --jellyfin-key "$KEY" --tmdb-key "$TMDB" --yes
```

## First hours

The first worker cycles download the embedding model, read your whole Jellyfin library, enrich
it from TMDB and embed it. On CPU that can take an hour or two for a large library. Follow it
with:

```bash
./muse status          # services, worker progress, catalog, queue
./muse status --url    # link to the status page
./muse logs embedding-worker
```

Then bring in history from elsewhere so Muse doesn't start cold:

```bash
./muse users                                          # your user ids (the Jellyfin user GUIDs)
./muse import letterboxd letterboxd-export.zip --user <id>
./muse import netflix ViewingActivity.csv --user <id> --profile "Your profile name"
./muse import youtube takeout.zip --user <id>
```

Kinds: `youtube`, `spotify`, `soundcloud`, `letterboxd`, `imdb`, `netflix`, `trakt`
([details](recommender.md#9-importing-history-on-onboarding)).

## Day to day

| | |
|---|---|
| `./muse status` | is everything healthy, how far is the worker |
| `./muse doctor` | finds problems and says how to fix them |
| `./muse upgrade` | pull, rebuild, restart (migrations run automatically) |
| `./muse backup` / `./muse restore FILE` | encrypted backups |
| `./muse export-user ID` / `./muse forget-user ID` | everything about one person, or delete it |
| `./muse rotate-keys` | new encryption key, re-encrypt all data |
| `./muse logs [service]`, `./muse restart [service]`, `./muse down`, `./muse up` | the usual |

## Optional parts

Enable them with `COMPOSE_PROFILES` in `.env` (comma separated), then `./muse up`:

| profile | what | needs |
|---|---|---|
| `curator` | autonomous 300 GB storage-budget manager (adds and removes titles through Radarr/Sonarr) | `RADARR_URL`/`SONARR_URL` in `.env`, keys in `secrets/radarr_api_key` / `secrets/sonarr_api_key` |
| `arr` | the arr-stack adapter (watchlist/rejection signal) | same |
| `searxng` | SearxNG search history adapter | `SEARXNG_UPSTREAM_URL` |
| `mcp` | MCP server so an assistant can ask you Muse's questions | `MUSE_MCP_USER_ID` |
| `tls` | HTTPS for other machines (set automatically in lan/tailscale mode) | |

GPU: setup detects an NVIDIA GPU and adds `docker-compose.gpu.yml` (needs the NVIDIA Container
Toolkit). It also offers an optional local Ollama for the LLM features.

## Connecting clients

* **Vesper**: Recommendation API URL + the Vesper read key (for recommendations, profile,
  evolution, questions), and the Ingestion API URL + the Vesper answer key (for answers and
  history uploads).
* **Jellyfin plugin**: Recommendation API URL + the plugin read key.
* In lan/tailscale mode the URLs are `https://<host>:8443/reco` and `https://<host>:8443/ingest`,
  and clients must trust Muse's certificate: `./muse ca > muse-ca.crt`.

## Troubleshooting

`./muse doctor` checks permissions, secrets, containers, exposure, migrations, the worker,
Jellyfin and TMDB, and explains each problem. Common ones:

* **Jellyfin unreachable from the adapter**: in local mode with Jellyfin on `127.0.0.1`, setup
  uses host networking for the adapter (`docker-compose.vesper.yml`). If Jellyfin runs in another
  Docker network, use its LAN address as `JELLYFIN_URL`.
* **401 from an API**: the client's key doesn't match `secrets/read_keys` (reads) or
  `secrets/adapter_keys` (writes). `./muse keys` shows the right ones.
* **503 "no read keys configured"**: `secrets/read_keys` is empty. Run `./muse setup` again.
* **Upgrading from an install before encryption**: `./muse upgrade` encrypts existing rows
  automatically (the admin step) and rebuilds derived data; nothing to do by hand.
