# Embedding worker

The background half of the taste engine (see [`docs/recommender.md`](../../docs/recommender.md)).
A loop, every `MUSE_EMBEDDING_INTERVAL_SECONDS` (default 300s):

| step | module | what |
|---|---|---|
| catalog | `catalog.py` | new events → `item` rows with canonical keys; episodes linked to their series |
| enrich | `enrich.py` | TMDB keywords/credits/collection/recommendations (`TMDB_API_KEY`), optional LLM tone/themes |
| canon | `catalog.py` | rebuild document + tags for titles whose items changed (`canon_item`) |
| vectors | `embed.py` | embed changed documents into Qdrant `muse_items`; payload-only updates; drop superseded keys |
| profiles | `profiles.py` | per-title signals → multi-interest profiles per domain (`taste_profile`), TMDB-related and household candidates, optional LLM interest names + summary |
| ranker | `ranker_fit.py` | daily: label impressions, re-fit ranker weights (`ranker_model`) |

`evaluate.py` replays your history offline and prints Recall/nDCG/diversity/calibration per
strategy (`docker compose exec embedding-worker python evaluate.py`).

Embedder and LLM are pluggable (local CPU/GPU, Ollama, vLLM, any OpenAI-compatible server,
Claude); see `.env.example` and `docs/recommender.md`. A model or dimension change re-embeds
the catalog automatically.

| env | default | |
|---|---|---|
| `MUSE_DATABASE_URL` | — | required |
| `MUSE_QDRANT_URL` | `http://qdrant:6333` | |
| `MUSE_EMBEDDING_INTERVAL_SECONDS` | `300` | |
| `MUSE_EMBED_PROVIDER` / `MUSE_EMBEDDING_MODEL` / `MUSE_EMBED_DEVICE` / `MUSE_EMBED_DIM` | `local` / `Qwen/Qwen3-Embedding-0.6B` / `auto` / native | |
| `MUSE_LLM_PROVIDER` | `none` | `ollama`, `vllm`, `openai`, `anthropic`, ... |
| `TMDB_API_KEY` | — | enrichment off without it |
| `MUSE_TASTE_LOOKBACK_DAYS` | `3650` | hard cutoff for events considered |
| `MUSE_FULL_PROFILE_HOURS` | `24` | everyone's profile refreshed (time decay) |
| `MUSE_RANKER_FIT_HOURS` | `24` | |
