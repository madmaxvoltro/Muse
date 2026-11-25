"""The synthetic world has known ground truth; the rewrite must beat the old pipeline on it."""

from muse_taste.evaluate import evaluate, ndcg_at_k, recall_at_k

from synthetic import user_history, world


def test_metrics_basic():
    assert recall_at_k(["a", "b", "c"], {"b", "z"}, 2) == 0.5
    assert ndcg_at_k(["a", "b"], {"a"}, 2) == 1.0


def test_new_pipeline_beats_legacy():
    vectors, genre_of, catalog, tags = world(per_genre=45)
    users = {f"u{s}": user_history(genre_of, seed=s) for s in (3, 4, 5)}
    res = evaluate(users, catalog, vectors, tags)
    print("\n" + res.table())
    m = res.per_strategy
    assert res.users == 3
    # The legacy pipeline is swamped by the 40-episode binge and progress polls.
    for strat in ("mean", "multi", "full", "full+rerank"):
        assert m[strat]["recall@10"] >= m["legacy"]["recall@10"] + 0.2, strat
        assert m[strat]["hit@10"] == 1.0, strat
    assert m["full+rerank"]["calib_kl"] <= m["full"]["calib_kl"] + 1e-9
    assert m["full+rerank"]["diversity"] >= m["full"]["diversity"]


def test_multi_interest_avoids_the_average_trap():
    """With content that sits between two loved genres, one mean vector recommends that
    in-between content; interest clusters don't (PinnerSage's motivating failure)."""
    vectors, genre_of, catalog, tags = world(per_genre=45, decoy=True)
    users = {f"u{s}": user_history(genre_of, seed=s) for s in (3, 4, 5)}
    res = evaluate(users, catalog, vectors, tags, strategies=("mean", "multi", "full+rerank"))
    print("\n" + res.table())
    m = res.per_strategy
    assert m["multi"]["recall@10"] > m["mean"]["recall@10"] + 0.1
    assert m["full+rerank"]["recall@10"] > m["mean"]["recall@10"] + 0.1
