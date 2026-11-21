# Muse security

Muse aggregates a behavioural profile across your media, searches and imported history. That
makes its data store the most sensitive thing in a homelab. The goal: **nothing personal Muse
stores is readable by anyone who gets hold of its files**: a database dump, a backup, a copied
Docker volume, a stolen disk, a log file. Personal data is only ever in plaintext inside the
running services that need it.

## What is protected, and how

### 1. Encryption at rest, inside Muse (`shared/muse_schema/crypto.py`)

Muse encrypts every personal field itself, before it reaches Postgres or Qdrant, so disk
encryption is not the only line of defence.

* **Sealed values**: AES-256-GCM (authenticated encryption), a random nonce per value, and the
  column's purpose as associated data. A ciphertext copied into another column or table does not
  decrypt, and tampering is detected.
* **Blind indexes**: values that must stay searchable or joinable (item ids, canonical title
  keys, dedupe keys, question dedupe keys) are stored as HMAC-SHA256 under a separate index key,
  namespaced per column. Equal values match; nothing else can be learnt. Because the key is
  secret, nobody can precompute "the index of *The Matrix*" from public TMDB ids. Vector-store
  point ids are derived from these blind keys for the same reason.
* **Key file**: `secrets/muse_master_key`, one index key plus one or more encryption keys
  (`./muse rotate-keys` adds a key and re-encrypts everything; old keys stay so old backups
  remain readable). Only the services that need it get it as a Docker secret. Adapters never do.
  Without the key every service refuses to start.

| stored | at rest |
|---|---|
| event metadata (titles, search queries, chat, imported rows, the source's item id) | sealed |
| event item id, dedupe key | blind index |
| event device context | sealed |
| item catalog metadata, TMDB enrichment, LLM descriptors | sealed; keys are blind |
| canonical titles (title, year, tags, document text, related titles, library ids, vote stats, real TMDB key) | sealed; key is blind |
| Qdrant payload (title, tags, collection, library ids, vote stats, real key) | sealed; point id and key blind |
| taste profiles (interests, loved/disliked titles, evolution, LLM summary) | sealed |
| questions and answers | sealed; dedupe key blind |
| pinned topics (overlay) | sealed |
| which download a person caused (curator) | blind index |
| uploaded history exports | never written to disk: parsed in memory, encrypted per event, dropped |
| backups (`./muse backup`) | the whole dump encrypted again (chunked AES-GCM; truncation and reordering detected) |

A test (`tests/integration/test_worker.py::test_nothing_readable_at_rest`) runs the full
pipeline, dumps every table and every Qdrant payload as text, and fails if any title, item id,
keyword, person or TMDB key appears.

### 2. Access control

* **Write-only adapters**: each adapter has its own key and can only add events for its own
  source (the server decides the source, not the request). It can't read anything back.
* **Read keys** for clients (Vesper, the Jellyfin plugin, MCP). Without read keys configured the
  recommendation API refuses every request (secure by default); `MUSE_ALLOW_OPEN_READ=true` is
  for development only. Keys are compared in constant time.
* **Least-privilege database roles** (`services/admin/admin.py`): every service connects as its
  own role. The ingestion API can insert events but can't read profiles. The recommendation
  API can read but can't delete or change events or profiles. The worker can't drop or create
  tables. A compromised service is limited to its own job. Tested in
  `tests/integration/test_admin_security.py`.
* **Qdrant API keys**: read-write for the worker, a read-only key for the recommendation API
  and the curator.
* **Status page**: counts and timestamps only, behind a token that moves from the URL into an
  HttpOnly cookie on first visit.

### 3. Network

* Three internal-by-default networks: `data` (Postgres, Qdrant and the services using them, no
  internet), `edge` (adapters reach only the ingestion API), and `egress` (outbound internet
  only for the worker, curator and adapters, to download models and reach TMDB or your
  Jellyfin/arr/SearxNG).
* The APIs listen on `127.0.0.1` only. For other machines, `./muse setup` enables the `tls`
  profile: Caddy serves both APIs over HTTPS with its own CA (`./muse ca` prints the root to
  trust), bound to your LAN or Tailscale address. Never `0.0.0.0`. `./muse doctor` flags it.
* Telemetry off: Qdrant, TimescaleDB and Hugging Face Hub (`DO_NOT_TRACK`).
* Outbound model endpoints outside your network (OpenAI, Anthropic, OpenRouter, a remote
  embedding server) are refused unless you explicitly allow them (`MUSE_*_ALLOW_EXTERNAL`).

### 4. Containers and secrets

* Secrets are files in `secrets/` (directory mode 0700), mounted as Docker secrets, never
  environment variables (which show up in `docker inspect`, crash reports and child processes).
* Every Muse container runs as an unprivileged user (uid 10001) with a read-only root
  filesystem, no Linux capabilities, `no-new-privileges`, and log files capped at 30 MB.
* Images for Postgres, Qdrant and Caddy are pinned.

### 5. Logs

No user ids with content, titles, search queries or keys in logs. HTTP access logs are off
(URLs contain user ids and `?mood=` text). Postgres never logs statement parameters.

## What this does not protect against

Being clear about the limits:

* **A compromised host while Muse runs.** Someone with root on the machine can read the key file
  and the memory of running services. Keep the host patched, isolated (own VLAN/LXC) and
  reachable only over Tailscale or SSH. Disk encryption (LUKS/ZFS) still helps against a stolen
  disk.
* **Activity metadata.** Event timestamps, action types (played, completed...), item types, the
  source name and the pseudonymous user id are stored in plaintext: they are needed for time
  decay and partitioning. Someone with a dump can see *that* user X did something at 21:00, not
  *what*.
* **Embedding inversion.** Item vectors in Qdrant are plaintext (similarity search needs them).
  Research shows text can be partly reconstructed from embeddings. Those vectors describe
  catalog titles (library content), not anyone's behaviour, and Qdrant is only reachable inside
  the `data` network with an API key. Taste profiles (derived from behaviour) are sealed in
  Postgres, not stored as vectors.
* **External services you enable.** TMDB sees which titles are looked up. An external LLM sees
  what it's sent (`MUSE_LLM_PERSONAL_TASKS=false` keeps your watched titles off it).
* **Losing the key.** Without `secrets/muse_master_key` the data cannot be read by anyone,
  including you. Back it up separately from your backups (a password manager, a USB stick).

## Operations

| task | command |
|---|---|
| everything Muse holds about one person, decrypted | `./muse export-user <id>` |
| delete everything about one person | `./muse forget-user <id>` |
| encrypted backup / restore (restore goes to a scratch database first and only swaps in on success) | `./muse backup`, `./muse restore <file>` |
| new encryption key + re-encrypt all data | `./muse rotate-keys` |
| check exposure, permissions, health | `./muse doctor` |

## Reporting

Found a problem? Open a private security advisory on the repository rather than a public issue.
