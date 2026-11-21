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
 request ─▶ 9 generators ─▶ RRF ─▶ features + score + uncertainty ─▶ calibrated list + explore
            muse_taste.retrieval     muse_taste.ranking                muse_taste.rerank
                                       │
      evolution (muse_taste.evolution): trends, satiation, rediscovery, rhythms, stretch
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
| A single vector search | Recall limited to "near the average". | Nine generators fused with Reciprocal Rank Fusion, including TMDB's "fans also liked" (borrowed collaborative filtering). |
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
| stretch | just beyond the edge of your 3 strongest interests (taste evolution, section 7) |

Fused with Reciprocal Rank Fusion (Cormack, Clarke & Büttcher, SIGIR 2009). It needs no
score calibration between generators, and titles several generators agree on rise.

## 5. Ranking: explainable score + uncertainty

`ranking.py` scores every candidate with a linear model over named features:

`interest` (importance-aware similarity to your best-matching interest), `short_term`,
`item_sim` (very close to a specific title you loved), `tags`, `negative` (repulsion),
`related`, `household`, `intent`, `quality` (IMDb-style Bayesian rating), `freshness` (new in
library), `fatigue` (shown on several days, never picked), `consensus` (several generators),
`mood`, and the taste-evolution features `trend`, `satiation`, `rediscovery`, `context` and
`stretch` (section 7).

Because the score is a sum of `weight × feature`, the explanation is exact. The API returns
the per-feature contributions (`explanation`, numeric, as the Jellyfin plugin expects) and
human reasons (`reasons`: "Because you like Slow-burn Nordic crime", "Similar to The Killing",
"Directed by Denis Villeneuve", "Fans of Arrival also liked this").

**Uncertainty** comes from a kernel-weighted count of your judged titles near the candidate
and whether they agree. Lots of consistent evidence nearby means a confident match. A region
you've never touched is a guess. `confidence` in the API is this, per candidate.

**Position bias.** People scan rows top-down, so a title shown 15th and ignored was probably
never seen. Impressions are weighted by examination propensity `(1/(1+position))^0.7`: picks
by 1/p (clipped), ignored titles by p, explicit thumbs-downs unchanged. This follows Joachims,
Swaminathan & Schnabel, ["Unbiased Learning-to-Rank with Biased Feedback"](https://arxiv.org/abs/1608.04468)
(WSDM 2017). In a simulation where the old ranker ordered by an irrelevant feature
(`tests/test_learn_elicit_providers.py`), naive learning gave that feature 52% of the weight of
true relevance; with the correction it's 17% (and slightly negative, an overcorrection of the
pointwise approximation).

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

## 7. Taste evolution

A profile is a snapshot; taste moves. `evolution.py` (computed by the worker with every
profile, applied per request) tracks how each interest changes and responds *subtly and
explainably*. One slider per user, `user_overlay.evolution_strength`: 0 = off, 1 = default,
2 = strong.

| what | how | effect on recommendations |
|---|---|---|
| **Lifecycle** per interest: emerging, growing, stable, fading, dormant | ratio of a short-memory (21 d) and long-memory (120 d) engagement rate, per interest. That's time-changing preferences after Koren, *Collaborative Filtering with Temporal Dynamics* (KDD 2009), at interest level; DIEN (Zhou et al., AAAI 2019) models interest evolution with a sequence model that needs far more data than a household | importance ±25%, `trend` feature, reason "You've been watching more X lately" |
| **Satiation** | leaky bucket of recent consumption (10 d half-life) against *your own* steady pace for that interest, amplified by declining engagement (abandons, thumbs down). Boredom follows intense consumption and recovers (Kapoor et al., *Just in Time Recommendations: Modeling the Dynamics of Boredom*, WSDM 2015). A binge alone isn't boredom, and a brand-new interest never counts as saturated | importance up to −40%, `satiation` feature (negative) |
| **Rediscovery** | interests that were once big (peak month share ≥10%) and dormant >120 days get a pull that grows with time away (Anderson et al., *The Dynamics of Repeat Consumption*, WWW 2014) | importance up to +60%, reason "It's been a while since your X phase" |
| **Rhythms** | per interest, lift by daypart, weekday/weekend and season (season only with ≥300 days of history), Dirichlet-smoothed toward your overall pattern; local time via `MUSE_TIMEZONE` | importance ∝ √lift, reason "You often pick X on weekend mornings" |
| **Stretch** (broadening) | each interest has a radius (where its members typically sit); the band just beyond it is the "adjacent possible". The band widens as you accept such suggestions (Beta(1,3) prior on acceptance) and narrows when you don't | a `stretch` generator, a small feature, and **explore slots prefer the frontier**, so exploration broadens your taste in small steps instead of being random (on serendipity vs accuracy: Ge et al., RecSys 2010; Kotkov et al., KBS 2016) |
| **Timeline** | monthly interest shares, breadth (effective number of interests), drift (how far the month's centre moved), milestones (new interest, a phase's peak, went quiet, biggest shift); optional LLM narrative | `GET /evolution/{user}` |

Profiles cluster on *undecayed* preference (structure) while importance uses decayed weight,
so an interest you loved two years ago remains its own (dormant) interest that can come back,
instead of being absorbed into whatever you watch now.

Measured on a synthetic household that moved from crime to animation over two months
(`tests/test_evolution.py`, 8 users): recall@10 0.175 → 0.238, nDCG@10 0.140 → 0.202, MRR 0.203 →
0.284, with slightly lower list diversity (rows lean toward the emerging interest, by design)
and hit@10 1.00 → 0.88 (one user's top 10 contained none of their held-out titles; within a
genre the synthetic titles are interchangeable). The evaluator's `full+evolution` strategy
measures the same on your own data.

## 8a. Watch again, listen again

`reconsume.py`. Everything else excludes what you've seen, but people rewatch comfort films
and replay favourite songs. Muse uses ACT-R's base-level activation, `B = ln Σ (t_now − t_j)^−0.5`,
which is high when an item was consumed recently and often. Recency and frequency were the
strongest single predictors of music relistening in Reiter-Haas et al., ["Predicting Music
Relistening Behavior Using the ACT-R Framework"](https://arxiv.org/abs/2108.02138) (RecSys 2021).
The same two drivers explain repeat consumption generally (Anderson et al., WWW 2014; Benson,
Kumar & Tomkins, WWW 2016).

* **Music**: high activation means it's on your mind, so it's likely wanted again.
* **Films/series**: the opposite. Only titles you clearly loved, at least 4 months since the last
  watch, and the pull grows as the memory fades, with a bonus for past rewatches. A Letterboxd or
  IMDb rating counts as a watch.

Shown as the "Watch again" / "Listen again" row with a reason ("You loved it, 2 years ago").

## 8b. Explanations you can correct

Every reason in `reason_details` has an id naming the part of your profile behind it
(`interest:<title>`, `anchor:<title>`, `tag:d:denis villeneuve`, `related:<title>`) and the
corrections a client may offer. This follows Balog, Radlinski & Arakelyan, ["Transparent, Scrutable
and Explainable User Models for Personalized
Recommendation"](https://research.google/pubs/transparent-scrutable-and-explainable-user-models-for-personalized-recommendation/)
(SIGIR 2019): users should be able to fix the *model*, not just one recommendation.

```bash
curl -X POST $INGEST/answers -H "X-Muse-Api-Key: $MUSE_KEY" -H 'content-type: application/json' \
  -d '{"kind":"reason","user_id":"<uuid>","reason_id":"anchor:<key>","response":"not_relevant"}'
```

| reason | responses | effect |
|---|---|---|
| `anchor:<title>` ("Similar to X") | not_relevant, more | stop basing recommendations on X (still counts as seen), or more like it |
| `related:<title>` ("Fans of X also liked") | not_relevant | same as above |
| `interest:<title>` ("Because you like …") | more, less | decaying steer on that interest |
| `tag:<tag>` ("Directed by …") | more, less | tag becomes a clear positive or negative |
| any | restore | undo |

Corrections apply on the very next request and are kept in the stored profile (`muted`, tag
overrides). Explanations are faithful by construction: each one is an actual top contribution
to the score. When Muse is unsure it says so ("Muse isn't sure about this one yet") rather than
sounding confident; honest uncertainty helps trust (Tintarev & Masthoff, 2012).

## 8c. Home screen rows

`GET /rows/{user}?item_type=movie` returns a page of themed rows whose titles are their
explanations: Top picks, Because you watched X (twice), For your weekend evenings (rhythms),
More <interest> (your three biggest), Watch again, From your watchlist, New in your library, and
Something different (the frontier of your taste plus uncertain picks). A title appears once per
page, short rows are dropped, and every item is logged with its row as the surface. This is the
page structure Netflix describes in Gomez-Uribe & Hunt, "The Netflix Recommender System"
(ACM TMIS 2015).

## 8. Muse asks questions (an optional queue)

Muse never depends on answers. The worker fills a **question queue** in the background
(`question_log`, migration 009); any client *may* fetch and answer from it, through the API or
the optional MCP server, and there's no interface of its own. Unanswered questions expire and
change nothing. Each entry has a priority (how much an answer would change recommendations),
a time to live, a re-ask window (the same question isn't queued again for months) and a
per-user cap of 12 pending (lowest priority expires first).

| kind | queued when | answers | effect of an answer |
|---|---|---|---|
| `onboarding` | little evidence yet (or one interest only) | loved / liked / disliked titles | ratings |
| `abandon` | a title stopped early, reason unknown | not_for_me, not_in_mood, too_slow, too_intense, will_finish_later, watched_elsewhere, bad_quality | "not in the mood" cancels the implicit negative, "not for me" makes it explicit |
| `interest` (emerging) | an interest is clearly rising | more / keep / less | decaying steer (60-day half-life) on that interest |
| `interest` (satiated) | satiation ≥ 0.5 | break / no_break | forces or suppresses satiation for ~2 weeks |
| `interest` (dormant) | a dormant interest with rediscovery pull | bring_back / moved_on | boosts or blocks rediscovery |
| `this_or_that` | always one, refreshed as answered/expired | a / b / both / neither / skip | small relative signals |
| `tag` (critiquing) | an attribute common among your best candidates that Muse knows little about | love / fine / not_for_me | tag override |

Onboarding titles are chosen by popularity-weighted farthest-point sampling, so few taps cover
the whole taste space (after Rashid et al., IUI 2002; Golbandi, Koren & Lempel, WSDM 2011).
Pairs are plausible, uncertain, *influential* and different from each other. Influence means
the answer informs many other plausible candidates (the active-learning criteria surveyed by
Elahi, Ricci & Rubens, Computer Science Review 2016), so an uncertain title in a dense region
is preferred over an isolated outlier. Pairwise questions with explore-exploit selection
learned preferences fastest in Christakopoulou, Radlinski & Hofmann, ["Towards Conversational
Recommender Systems"](https://www.microsoft.com/en-us/research/publication/towards-conversational-recommender-systems/)
(KDD 2016). Tag questions are system-suggested critiques (Chen & Pu, "Critiquing-based
recommenders: survey and emerging trends", UMUAI 2012): one tap on an attribute shifts many
candidates at once. Interest answers are matched to
today's interests by their anchor title, because interests are re-clustered every cycle.

| read (recommendation API) | |
|---|---|
| `GET /questions/{user}/queue?limit=5&domain=video&kind=interest` | queued questions, most valuable first; each includes `answer_with.body`, the exact body to POST; returned pending entries are marked served |
| `GET /questions/{user}/next` | top of the queue, or one generated on the spot if the queue is empty |
| `GET /questions/{user}/onboarding` · `/this-or-that` · `/abandoned` | on-demand generators (not queued) |

```bash
# answers go to the ingestion API with the `muse` key
curl -X POST $INGEST/answers -H "X-Muse-Api-Key: $MUSE_KEY" -H 'content-type: application/json' \
  -d '{"kind":"interest","user_id":"<uuid>","question_id":"<id>","response":"more"}'
curl -X POST $INGEST/answers ... -d '{"kind":"this_or_that","user_id":"<uuid>","question_id":"<id>","a":"tmdb:movie:1","b":"tmdb:tv:2","answer":"a"}'
curl -X POST $INGEST/answers ... -d '{"kind":"abandon","user_id":"<uuid>","question_id":"<id>","canon_key":"tmdb:movie:3","reason":"not_in_mood"}'
curl -X POST $INGEST/answers ... -d '{"kind":"onboarding","user_id":"<uuid>","question_id":"<id>","loved":["tmdb:movie:603"],"disliked":["tmdb:movie:13"]}'
curl -X POST $INGEST/answers ... -d '{"kind":"dismiss","user_id":"<uuid>","question_id":"<id>"}'   # no taste data written
```

**MCP server** (`services/mcp-server`, optional, `docker compose --profile mcp up -d mcp-server`):
tools `muse_pending_questions`, `muse_answer_question`, `muse_dismiss_question`,
`muse_recommendations`, `muse_taste_profile`, `muse_taste_evolution`. Point any MCP client at
`http://127.0.0.1:8002/mcp` (streamable HTTP), or run `python server.py` with stdio. The
server's instructions tell the assistant to ask at most one question now and then, in your
language, and never insist. Without `MUSE_MCP_ANSWER_KEY` it is read-only.

## 9. Importing history on onboarding

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

## 10. Models and hardware

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

## 11. Is it getting better? Evaluation

```bash
docker compose exec embedding-worker python evaluate.py            # all domains
docker compose exec embedding-worker python evaluate.py --domain video --k 20
# (prints aggregate metrics only; nothing personal)
```

Temporal leave-future-out per user (random splits leak the future; see Ji et al., *A
Critical Study on Data Leakage in Recommender System Offline Evaluation*, TOIS 2023).
Profiles are rebuilt from history before the cut and scored on what you went on to like.
Strategies: `legacy` (v1), `mean`, `multi`, `full`, `full+rerank`, `full+evolution`, with Recall/nDCG/MRR/Hit@K,
intra-list diversity, calibration KL and coverage. Run it before and after changing a
weight, the model or a threshold. Each user needs ≥5 liked titles.

Live, `/profile/{user}` shows what Muse thinks your taste is (interests with examples and
names, loved and disliked titles, top tags, summary). If it looks wrong, the
recommendations will too, so it's the first place to look.

## 12. Tuning knobs

| env | default | effect |
|---|---|---|
| `MUSE_TMDB_ITEMS_PER_CYCLE` | 150 | enrichment speed |
| `MUSE_RELATED_PER_ITEM` | 12 | TMDB recommendations added to the catalog per title |
| `MUSE_LLM_ITEMS_PER_CYCLE` | 40 | LLM descriptor speed |
| `MUSE_FULL_PROFILE_HOURS` | 24 | full profile refresh (time decay) |
| `MUSE_RANKER_FIT_HOURS` | 24 | weight re-fit |
| `MUSE_CURATOR_ADD_SCORE` / `_REMOVE_SCORE` | 0.55 / 0.10 | curator thresholds, in ranker score units; preview candidates with `/recommendations/{user}?include_unavailable=true` |
| `user_overlay.explore_ratio` | 0.125 | explore share per user |
| `user_overlay.evolution_strength` | 1.0 | taste evolution: 0 off, 1 subtle, 2 strong |
| `MUSE_TIMEZONE` | UTC | local time for rhythms |

Half-lives, abandon rules and default weights are named constants at the top of
`signals.py` / `ranking.py` / `rerank.py`.

## 13. Upgrading from v1

1. Migrations apply themselves (the `admin` step runs on every start), and rows written before
   encryption are encrypted automatically.
2. Rebuild and restart `embedding-worker`, `recommendation-api`, `ingestion-api`, `curator`,
   `jellyfin-adapter`. The first worker cycles download the embedding model, build the
   catalog from the whole event log, enrich it (if `TMDB_API_KEY` is set) and embed it. That
   takes a while for a large library; progress is in the worker log.
3. The old Qdrant collections `content_items` and `taste_vectors` are no longer used and can
   be deleted.
4. `./muse setup` creates the `muse:` (answers) and `import:` keys in `secrets/adapter_keys`;
   `./muse keys` shows them.

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
