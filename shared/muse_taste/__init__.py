"""muse_taste — Muse's recommendation engine as a pure library.

No database or vector-store access lives here: the embedding worker, the recommendation API,
the curator, the offline evaluator and the tests all run the same functions on in-memory
data. See docs/recommender.md for the design and the research it builds on.

Modules: canon (identity), signals (events -> per-title judgement), documents (item text +
tags), profile (multi-interest taste), retrieval (generators + fusion), ranking (features,
score, uncertainty), rerank (calibration, diversity, exploration), elicitation (questions),
learn (weights from impressions), evaluate (offline metrics), importers (history exports),
embedder / llm / tmdb (pluggable providers), privacy (outbound-call guard).

Submodules are imported explicitly (`from muse_taste import signals`) so services that only
need the stdlib parts (the ingestion API's importers) don't pull in numpy.
"""

__version__ = "0.2.0"
