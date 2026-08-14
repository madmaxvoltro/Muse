# Embedding worker

Status: not yet implemented.

Batch job that reads new rows from the `event` table, computes/updates:
- **content embeddings** per item (local model on metadata now, transcripts/subtitles later)
- **taste embeddings** per user, multi-vector per `item_type` + one cross-domain vector, time-decayed

and writes vectors into Qdrant. See `../../docs/architecture.md`.
