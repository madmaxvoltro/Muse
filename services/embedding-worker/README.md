# Embedding worker

Status: implemented (v1 — metadata-only text embeddings; transcripts/subtitles are a
follow-up, see `docs/architecture.md`).

Standalone loop (not a web service) that every `MUSE_EMBEDDING_INTERVAL_SECONDS` (default 300s):

1. Reads events written since its last run, builds a text blob per distinct item from
   `metadata` (title/description/genres/query), embeds it with a local
   `sentence-transformers` model (`all-MiniLM-L6-v2` by default — no external API calls),
   and upserts into the Qdrant `content_items` collection.
2. For every user touched by those new events, recomputes their taste vector(s): a
   weighted average of the content vectors of everything they interacted with in the last
   `MUSE_TASTE_LOOKBACK_DAYS`, weighted by `action_weight * exp(-age_days / MUSE_TASTE_DECAY_HALFLIFE_DAYS)`.
   Computed per `item_type` plus one `_combined` cross-domain vector, written to the Qdrant
   `taste_vectors` collection.

Progress is tracked in the `embedding_worker_state` table (`db/migrations/002_embedding_state.sql`)
so each cycle only processes events since the last one.

## Env vars

| var | default | meaning |
|---|---|---|
| `MUSE_DATABASE_URL` | — | required |
| `MUSE_QDRANT_URL` | `http://qdrant:6333` | |
| `MUSE_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | any local sentence-transformers model |
| `MUSE_EMBEDDING_INTERVAL_SECONDS` | `300` | |
| `MUSE_TASTE_DECAY_HALFLIFE_DAYS` | `60` | how fast old interactions stop mattering |
| `MUSE_TASTE_LOOKBACK_DAYS` | `730` | hard cutoff, mostly a query-cost bound |

## Known v1 limitations

- Items with no text in `metadata` (title/description/genres/query all empty) are skipped —
  an adapter needs to send at least a title for its items to ever get recommended.
- Taste-vector recompute re-reads a user's full lookback window every cycle rather than
  incrementally updating — fine at personal-library scale, would need revisiting if this were
  ever multi-tenant at real scale.
