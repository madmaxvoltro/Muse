# Muse taste engine

How Muse understands content, builds your taste profile, finds candidates, ranks them, asks
you questions, and checks it's getting better. The engine is one pure-Python library,
[`shared/muse_taste`](../shared/muse_taste), used by the embedding worker, the recommendation
API, the curator, the offline evaluator and the tests, so they all run the same code.

```
 events ─▶ catalog ─▶ canonical titles ─▶ enrichment ─▶ documents + tags ─▶ embeddings (Qdrant)
 (all       (item)     (canon_item)        TMDB, LLM      muse_taste.documents     │
 sources)                                                                          ▼
          per-title signals ─▶ multi-interest profile (taste_profile) ◀────────────┘
          muse_taste.signals    muse_taste.profile
                                       │
 request ─▶ 8 generators ─▶ RRF ─▶ features + score + uncertainty ─▶ calibrated list + explore
            muse_taste.retrieval     muse_taste.ranking                muse_taste.rerank
                                       │
                         impressions ─▶ weight learning (muse_taste.learn)
```

## What changed from v1, and why

| v1 | problem | now |
|---|---|---|
| Taste vector = mean of every event's `action_weight` | A film polled every 5 minutes gave ~24 `played` events (7× a completed watch); a 60-episode binge gave 60. The profile measured adapter chattiness, not taste. | One judgement per title (`signals.py`); episodes roll up into their series with a saturating curve. |
| One averaged vector per item type | Loving crime *and* animation gives a vector halfway between, close to neither. | Several interests per person, each with a medoid and an importance (PinnerSage). |
| Dislikes averaged into the same vector | A thumbs-down only tilted the average. | Disliked titles are kept as negative prototypes and repel similar candidates. |
| `cataloged` (library) events counted as interest | Genre "popularity" and confidence grew with library size. | `cataloged` is ignored for taste. |
| `exp(-age/60)` called a half-life | It decays to 1/e, not 1/2. | Real half-lives (120 days implicit, 540 days explicit, with a floor). |
| Item text = title + overview + genres, all-MiniLM (English, short) | Thin content understanding. No people, keywords or tone. | TMDB keywords, credits, collection, language + optional LLM tone/themes; Qwen3-Embedding (multilingual); exact-match tags next to the embedding. |
| Each source's IDs kept separate | A 5★ Letterboxd film and the same film in Jellyfin were unrelated; titles watched on Netflix were re-recommended. | Canonical identity (`tmdb:movie:603`...) across all sources. |
| A single vector search | Recall limited to "near the average". | Eight generators fused with Reciprocal Rank Fusion, including TMDB's "fans also liked" (borrowed collaborative filtering). |
| Confidence per genre; explore = `confidence < 0.5` | Explore slots never learned anything specific. | Per-candidate uncertainty from nearby evidence; Thompson sampling; impressions logged. |
| Diversity = max 3 per genre | Crude, and blind to proportions. | Calibration to your interest mix (Steck) + redundancy penalty (MMR) + one entry per franchise. |
| No evaluation | No way to tell whether a change helped. | Temporal offline evaluation on your own data (`evaluate.py`). |

On a synthetic household with known ground truth (`tests/test_evaluate.py`), the v1 pipeline
scores recall@10 = 0.00 (swamped by one binge and progress polls) against 0.30–0.43 for the
new engine. When there's content sitting *between* two loved genres, a single mean vector
gets 0.03 against 0.30–0.33 for the multi-interest profile. Synthetic numbers only show the
mechanisms work. Run the evaluator on your own data to see real numbers (below).

## 1. Content understanding

**Canonical identity** (`canon.py`): each title gets the strongest key known (TMDB, then
IMDb/TVDB, then normalised artist|title for music, YouTube video id...). The worker upgrades
weak keys once TMDB enrichment resolves them, so a Netflix row "Dark: Season 1: Secrets" and a
Letterboxd rating by title + year both land on `tmdb:tv:70523` / `tmdb:movie:...`.

**Enrichment** (`tmdb.py`, worker `enrich.py`): per film/series, one TMDB request with
keywords, credits (directors, writers, top cast), companies/networks, collection, language,
vote stats and TMDB's *recommendations*. Those recommendations become catalog items too
(embedded, not in your library), the pool for "fans of X also liked Y" and for the curator.
TMDB sees which titles are looked up, never who watched what. Cached, refreshed monthly.

**Documents and tags** (`documents.py`): two complementary views of every title:
- a natural-language document for the embedder, most discriminative first (title, type,
  genres, tone, themes, keywords, tagline, overview, people);
- namespaced exact tags (`d:denis villeneuve`, `k:time loop`, `c:<collection>`, `lang:ja`...),
  weighted by namespace (a shared director says more than a shared decade) and by IDF.

**Optional LLM descriptors** (`llm.py`): "slow-burn, bleak, dry humour" plus themes. They go
into the document because overviews say what *happens* and taste is mostly about how a work
*feels*. This follows recent work on LLM-generated item/user profiles for recommendation
(e.g. [Language-Based User Profiles for Recommendation](https://arxiv.org/abs/2402.15623),
Dai, Zhou & Joachims).

## 2. Signals: from events to one judgement per title

`signals.py` reduces a person's events per canonical title to **preference** (signed) and
**confidence** (evidence mass). That split follows Hu, Koren & Volinsky, *Collaborative
Filtering for Implicit Feedback Datasets* (ICDM 2008): watching to the end is strong
evidence, a detail-page view weak.

- Explicit beats implicit: the latest thumbs wins; clearing a rating falls back to viewing.
- Completion ≥85% counts as watched (credits); rewatches add log-scaled preference.
- Series: `1.4·(1 − e^(−episodes/4))`, so episode 1 says a lot and episode 40 little; dropped after the pilot = mild negative.
- Abandon: stopped below 60% and untouched for 14 days = mild negative, stronger the earlier
  you quit, unless you said why (see questions): "not in the mood" cancels it, "not for me"
  makes it explicit.
- Music: listens log-scaled, mostly-skipped tracks negative.
- Watchlist / request / trailer = *intent*: feeds the backlog generator, not "seen".
- Your overlay sliders (`user_overlay.source_weights`, `disabled_sources`) apply here, on the
  read path; stored events are never changed.

## 3. Taste profile: several interests, not one average

`profile.py`, after PinnerSage ([Pal et al., KDD 2020](https://arxiv.org/abs/2007.03634)):

1. Positive titles are clustered with Ward agglomerative clustering. The cut adapts: the
   smallest number of clusters whose members are coherent enough (similarity to their
   centroid, normalised to the model's baseline). A light user gets 1–3 interests, a heavy
   one 10+.
2. Each interest keeps its **medoid** (the most central real title, used for retrieval
   because it can't drift into empty space between clusters), its centroid (used for
   scoring), its **importance** (sum of decayed preference) and its top tags. With an LLM it
   also gets a name ("Slow-burn Nordic crime").
3. **Negative prototypes**: up to 64 disliked titles as individual repulsors. Wang et al.,
   [Learning from Negative User Feedback and Measuring Responsiveness](https://arxiv.org/abs/2308.12256)
   (RecSys 2023), show negatives need their own "not-to-recommend" term instead of being
   folded into the positive objective.
4. **Short-term intent**: the last 14 days with a 3-day half-life, separate from long-term taste.
5. **Tag affinities**: per tag, shrunk mean preference × log support × IDF × namespace weight.
6. **Baseline**: every embedding model has its own similarity between unrelated items
   (anisotropy). All similarities are normalised against it, so weights mean the same thing
   whichever model you configure.
7. **Pinned topics** (`user_overlay.pinned_topics`, e.g. "space opera") become interests of
   their own with a fixed share.

The worker also precomputes per profile the TMDB-related candidates (titles TMDB recommends
from your strongest titles, with which titles vouched for them) and household candidates
(user-user agreement on ≥3 shared titles; others' liked titles vote).

## 4. Candidate generation

`retrieval.py` + the API's `engine.py`. Each generator fails differently, so together they
recall far more than a single query:

| generator | what |
|---|---|
| interests | one ANN query per interest medoid, slots ∝ importance (min 12) |
| short_term | ANN on recent intent |
| item2item | ANN around your 6 strongest recent titles |
| related | TMDB recommendations of your top titles (collaborative signal from millions of viewers) |
| household | liked by similar-tasting people in your household |
| backlog | watchlist / requested / trailer watched |
| mood | ANN on `?mood=cosy+and+funny` (needs an embedder in the API) |
| quality | best Bayesian-rated titles (cold start, safety net) |

Fused with Reciprocal Rank Fusion (Cormack, Clarke & Büttcher, SIGIR 2009). It needs no
score calibration between generators, and titles several generators agree on rise.

## 5. Ranking: explainable score + uncertainty

`ranking.py` scores every candidate with a linear model over named features:

`interest` (importance-aware similarity to your best-matching interest), `short_term`,
`item_sim` (very close to a specific title you loved), `tags`, `negative` (repulsion),
`related`, `household`, `intent`, `quality` (IMDb-style Bayesian rating), `freshness` (new in
library), `fatigue` (shown on several days, never picked), `consensus` (several generators),
`mood`.

Because the score is a sum of `weight × feature`, the explanation is exact. The API returns
the per-feature contributions (`explanation`, numeric, as the Jellyfin plugin expects) and
human reasons (`reasons`: "Because you like Slow-burn Nordic crime", "Similar to The Killing",
"Directed by Denis Villeneuve", "Fans of Arrival also liked this").

**Uncertainty** comes from a kernel-weighted count of your judged titles near the candidate
and whether they agree. Lots of consistent evidence nearby means a confident match. A region
you've never touched is a guess. `confidence` in the API is this, per candidate.

**Learning the weights** (`learn.py`, worker `ranker_fit.py`, daily): impressions are labelled
with what happened in the next 7 days (played/finished/thumbs up = positive; thumbs down /
"not for me" = strong negative; ignored = weak negative). It's logistic regression with a
Gaussian prior centred on the hand-set weights (MAP), so little data keeps them and lots of
consistent data moves them. New weights are adopted only if they beat the current ones on a
later, held-out slice of time. Sign constraints keep `negative` and `fatigue` from flipping.

## 6. Building the list

`rerank.py`, one greedy pass maximising
`(1−λc)·score + λc·calibration_gain − λr·redundancy`:

- **Calibration**: Steck, [Calibrated Recommendations](https://doi.org/10.1145/3240323.3240372)
  (RecSys 2018). The list's mix over your *interests* should match their importance (70/30 →
  ~70/30). Calibrating over interest clusters instead of genres follows Kaya & Bridge
  (RecSys 2019), who found intent-aware selection over sub-profiles the best
  relevance/diversity trade-off. Membership carries relevance as mass, and an explicit
  "other" bucket with a small target share stops off-taste items from "balancing" a list.
- **Redundancy**: MMR (Carbonell & Goldstein, 1998), max similarity to what's already picked.
  (DPPs, [Wilhelm et al., CIKM 2018](https://doi.org/10.1145/3269206.3272018), were considered;
  MMR + calibration is simpler and explainable at household scale.)
- **Franchises**: one entry per TMDB collection, the earliest part you haven't seen.
- **Exploration**: Thompson sampling (Chapelle & Li, NeurIPS 2011). Each uncertain,
  still-promising candidate's score is drawn from N(score, σ²), the best draws fill the explore
  slots (default 12.5%, `explore_ratio` / overlay), interleaved through the row rather than
  at the end. Draws are seeded per user, surface and day, so rows are stable within a day and
  rotate between days.

Feedback is immediate: events newer than the stored profile are folded in at request time,
so a thumbs-down removes and repels similar titles on the very next request.

## 7. Muse asks questions

Read side: recommendation API. Answers: ingestion API (`muse` key).

| endpoint | what |
|---|---|
| `GET /questions/{user}/next?item_type=movie` | the single most useful question now: onboarding while Muse knows little, then "why did you stop?", then this-or-that |
| `GET /questions/{user}/onboarding?n=24` | well-known titles spread across the catalog (popularity-weighted farthest-point sampling): few taps cover the whole taste space. After Rashid et al. (IUI 2002) and Golbandi, Koren & Lempel (WSDM 2011) on cold-start elicitation |
| `GET /questions/{user}/this-or-that` | a pair that is plausible, uncertain and different (uncertainty sampling from active learning; pairwise answers are easier and more consistent than ratings) |
| `GET /questions/{user}/abandoned` | titles stopped early with no known reason |

```bash
# answers (X-Muse-Api-Key: the `muse` key)
curl -X POST $INGEST/answers -H "X-Muse-Api-Key: $MUSE_KEY" -H 'content-type: application/json' \
  -d '{"kind":"onboarding","user_id":"<uuid>","question_id":"<id>","loved":["tmdb:movie:603"],"liked":[],"disliked":["tmdb:movie:13"]}'
curl -X POST $INGEST/answers ... -d '{"kind":"this_or_that","user_id":"<uuid>","question_id":"<id>","a":"tmdb:movie:1","b":"tmdb:tv:2","answer":"a"}'   # a|b|both|neither|skip
curl -X POST $INGEST/answers ... -d '{"kind":"abandon","user_id":"<uuid>","question_id":"<id>","canon_key":"tmdb:movie:3","reason":"not_in_mood"}'
```

Abandon reasons: `not_for_me`, `not_in_mood`, `too_slow`, `too_intense`, `will_finish_later`,
`watched_elsewhere`, `bad_quality`.

## 8. Importing history on onboarding

`POST /import/{kind}?user_id=<uuid>&filename=<name>` on the ingestion API with the export file
(or the whole .zip) as the body, using the `muse` or `import` key. Or use the CLI:

```bash
MUSE_IMPORT_KEY=... python adapters/importer/import_history.py --kind letterboxd --user <uuid> letterboxd-export.zip
```

| kind | export | read |
|---|---|---|
| youtube | Google Takeout → YouTube and YouTube Music (choose **JSON**) | watch history (YouTube Music → tracks), searches, subscriptions, liked videos |
| spotify | Privacy settings → Extended streaming history (or account data) | plays (≥30 s), skips, liked songs |
| letterboxd | Settings → Data → Export | ratings, diary, watched, watchlist, likes |
| imdb | Your Ratings / Watchlist → Export | ratings (with IMDb ids), watchlist |
| netflix | Account → Download your personal information | ViewingActivity.csv (`&profile=Name` for shared accounts; trailers skipped) |
| trakt | JSON history / ratings / watchlist (API shapes) | with TMDB/IMDb ids |
| soundcloud | best effort: CSV/JSON with title + artist, or a list of track URLs | likes |

5-star scales: ≥4.5 love, ≥3.5 up, ≤2 down, in between "watched". 10-point scales: ≥9 love,
≥7 up, ≤4 down. Every event carries a dedupe key, so re-importing is a no-op.

## 9. Models and hardware

All of this is configuration (`.env`, shared by worker, curator and API via `x-embedding-env`):

| setup | settings |
|---|---|
| CPU, ≥16 GB (default) | `MUSE_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B` (multilingual, strong, ~2.4 GB RAM on CPU) |
| CPU, tight | `BAAI/bge-small-en-v1.5` or `MUSE_EMBED_DIM=512` |
| NVIDIA GPU | `docker-compose.gpu.yml` + `--build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124`; device auto-detected, fp16 |
| Ollama / vLLM / TEI / LM Studio | `MUSE_EMBED_PROVIDER=openai`, `MUSE_EMBED_BASE_URL=http://ollama:11434/v1`, `MUSE_EMBEDDING_MODEL=qwen3-embedding:0.6b` |
| LLM local | `MUSE_LLM_PROVIDER=ollama` (or vllm/lmstudio/openai-compatible), `MUSE_LLM_MODEL=qwen3:8b` |
| LLM Claude / ChatGPT | `MUSE_LLM_PROVIDER=anthropic` (official SDK, default `claude-opus-5-5`) or `openai`; requires `MUSE_LLM_ALLOW_EXTERNAL=true` |

Privacy rule (`privacy.py`): model endpoints inside your network are fine; anything else is
refused unless explicitly allowed. With an external LLM, `MUSE_LLM_PERSONAL_TASKS=false` keeps
interest naming and the taste summary (which send watched titles) off it. Item descriptions
only send public metadata.

Changing the model or dimension re-embeds the catalog automatically (the model is part of
every document hash).

## 10. Is it getting better? Evaluation

```bash
docker compose exec embedding-worker python evaluate.py            # all domains
docker compose exec embedding-worker python evaluate.py --domain video --k 20
```

Temporal leave-future-out per user (random splits leak the future; see Ji et al., *A
Critical Study on Data Leakage in Recommender System Offline Evaluation*, TOIS 2023).
Profiles are rebuilt from history before the cut and scored on what you went on to like.
Strategies: `legacy` (v1), `mean`, `multi`, `full`, `full+rerank`, with Recall/nDCG/MRR/Hit@K,
intra-list diversity, calibration KL and coverage. Run it before and after changing a
weight, the model or a threshold. Each user needs ≥5 liked titles.

Live, `/profile/{user}` shows what Muse thinks your taste is (interests with examples and
names, loved and disliked titles, top tags, summary). If it looks wrong, the
recommendations will too, so it's the first place to look.

## 11. Tuning knobs

| env | default | effect |
|---|---|---|
| `MUSE_TMDB_ITEMS_PER_CYCLE` | 150 | enrichment speed |
| `MUSE_RELATED_PER_ITEM` | 12 | TMDB recommendations added to the catalog per title |
| `MUSE_LLM_ITEMS_PER_CYCLE` | 40 | LLM descriptor speed |
| `MUSE_FULL_PROFILE_HOURS` | 24 | full profile refresh (time decay) |
| `MUSE_RANKER_FIT_HOURS` | 24 | weight re-fit |
| `MUSE_CURATOR_ADD_SCORE` / `_REMOVE_SCORE` | 0.55 / 0.10 | curator thresholds, in ranker score units; preview candidates with `/recommendations/{user}?include_unavailable=true` |
| `user_overlay.explore_ratio` | 0.125 | explore share per user |

Half-lives, abandon rules and default weights are named constants at the top of
`signals.py` / `ranking.py` / `rerank.py`.

## 12. Upgrading from v1

1. Apply the migration on an existing database:
   `docker compose exec -T postgres psql -U muse -d muse < db/migrations/007_taste_engine.sql`
2. Rebuild and restart `embedding-worker`, `recommendation-api`, `ingestion-api`, `curator`,
   `jellyfin-adapter`. The first worker cycles download the embedding model, build the
   catalog from the whole event log, enrich it (if `TMDB_API_KEY` is set) and embed it. That
   takes a while for a large library; progress is in the worker log.
3. The old Qdrant collections `content_items` and `taste_vectors` are no longer used and can
   be deleted.
4. Add a `muse:` key (for the client showing questions) and optionally an `import:` key to
   `MUSE_ADAPTER_KEYS`.

## Known limits

- Tested end to end against Postgres and an in-memory Qdrant with a stand-in embedder and a
  stand-in TMDB, not yet against a real Jellyfin library, the real Qwen3 model or the live
  TMDB API. Thresholds (cluster coherence, curator scores) may need a tune on real data; the
  evaluator and `/profile` are how to tell.
- SoundCloud has no documented export format; that importer is deliberately lenient. YouTube's
  HTML Takeout is parsed best-effort; choose JSON.
- Music and books have no TMDB-like enrichment yet (MusicBrainz / Open Library would be the
  next step); they work on adapter metadata + embeddings.
- Household CF needs ≥3 titles judged by both people before it trusts the overlap.
