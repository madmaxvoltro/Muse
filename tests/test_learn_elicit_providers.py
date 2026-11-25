import numpy as np
import pytest

from muse_taste.elicitation import onboarding_spread, this_or_that
from muse_taste.learn import auc, fit_weights
from muse_taste.llm import from_env as llm_from_env, parse_json_object
from muse_taste.privacy import ExternalEndpointNotAllowed, is_private_host, require_private_or_allowed
from muse_taste.profile import build_profile
from muse_taste.ranking import DEFAULT_WEIGHTS, CandidateInput, score_candidates
from muse_taste.signals import aggregate
from muse_taste.tmdb import parse_details

from synthetic import NOW, user_history, world


def test_auc():
    assert auc(np.array([0.1, 0.9, 0.8, 0.2]), np.array([0, 1, 1, 0])) == 1.0
    assert auc(np.array([0.5, 0.5]), np.array([0, 1])) == 0.5


def test_fit_weights_needs_data_then_learns():
    names = ["interest", "quality", "fatigue"]
    small = fit_weights(names, np.zeros((10, 3)), np.zeros(10), np.ones(10), np.arange(10), DEFAULT_WEIGHTS)
    assert not small.adopted
    rng = np.random.default_rng(0)
    X = rng.standard_normal((2000, 3))
    # truth: quality matters far more than the hand-set weight says
    logit = 0.5 * X[:, 0] + 2.5 * X[:, 1] - 0.5 * X[:, 2]
    y = (rng.random(2000) < 1 / (1 + np.exp(-logit))).astype(float)
    res = fit_weights(names, X, y, np.ones(2000), np.arange(2000), DEFAULT_WEIGHTS)
    assert res.adopted and res.weights["quality"] > DEFAULT_WEIGHTS["quality"] * 2
    assert res.auc_learned > res.auc_default


def test_onboarding_covers_genres():
    vectors, genre_of, catalog, _ = world(per_genre=30)
    keys = list(vectors)
    V = np.stack([vectors[k] for k in keys])
    picks = onboarding_spread(keys, V, np.ones(len(keys)) * 100, n=12)
    assert len({genre_of[k] for k in picks}) == 6


def test_this_or_that_asks_where_uncertain():
    vectors, genre_of, catalog, tags = world()
    sig = aggregate(user_history(genre_of), now=NOW)
    prof = build_profile("u", "video", sig, vectors, tags, now=NOW)
    pool = score_candidates(prof, [CandidateInput(key=k, vector=vectors[k], tags=tags[k]) for k in vectors if k not in sig], anchor_vectors=vectors)
    a, b = this_or_that(pool, prof.baseline)
    assert a.key != b.key
    assert genre_of[a.key] != genre_of[b.key] or a.interest_id != b.interest_id


def test_privacy_guard():
    assert is_private_host("ollama") and is_private_host("192.168.1.5") and is_private_host("100.101.1.1") and is_private_host("box.ts.net")
    assert not is_private_host("8.8.8.8", resolve=False)
    with pytest.raises(ExternalEndpointNotAllowed):
        require_private_or_allowed("https://8.8.8.8/v1", "false", "LLM endpoint")
    require_private_or_allowed("https://8.8.8.8/v1", "true", "LLM endpoint")
    with pytest.raises(ExternalEndpointNotAllowed):
        llm_from_env({"MUSE_LLM_PROVIDER": "anthropic", "MUSE_LLM_API_KEY": "x"})
    assert llm_from_env({}) is None


def test_parse_json_object():
    assert parse_json_object('```json\n{"label": "Cosy fantasy"}\n```') == {"label": "Cosy fantasy"}
    assert parse_json_object('Sure! {"a": {"b": 1}} trailing') == {"a": {"b": 1}}
    assert parse_json_object("nope") is None


def test_tmdb_parse_movie():
    d = {
        "id": 329865, "title": "Arrival", "release_date": "2016-11-10", "overview": "o", "genres": [{"id": 18, "name": "Drama"}],
        "keywords": {"keywords": [{"name": "alien contact"}]},
        "credits": {"crew": [{"name": "Denis Villeneuve", "job": "Director", "department": "Directing"}, {"name": "Eric Heisserer", "job": "Screenplay", "department": "Writing"}],
                    "cast": [{"name": "Amy Adams", "order": 0}]},
        "belongs_to_collection": None, "vote_average": 7.6, "vote_count": 18000,
        "recommendations": {"results": [{"id": 1, "title": "Interstellar", "genre_ids": [18], "release_date": "2014-11-05"}]},
    }
    e = parse_details("movie", d, {18: "Drama"})
    assert e["directors"] == ["Denis Villeneuve"] and e["writers"] == ["Eric Heisserer"] and e["keywords"] == ["alien contact"]
    assert e["recommendations"][0]["genres"] == ["Drama"] and e["recommendations"][0]["year"] == 2014


def test_ips_corrects_position_bias():
    """The old ranker ordered by an irrelevant feature; people only look at the top. Naive
    learning credits that feature; propensity weighting mostly removes the credit."""
    from muse_taste.learn import _fit_logistic, examination_propensity, ips_sample_weight

    rng = np.random.default_rng(0)
    X, y, w_naive, w_ips = [], [], [], []
    for _ in range(600):  # 600 lists of 10
        rel = rng.standard_normal(10)
        biased = rng.standard_normal(10)
        order = np.argsort(-biased)  # shown in order of the irrelevant feature
        for pos, i in enumerate(order):
            seen = rng.random() < examination_propensity(pos)
            click = seen and rng.random() < 1 / (1 + np.exp(-(2.0 * rel[i] - 1.0)))
            label = 1.0 if click else 0.0
            X.append([rel[i], biased[i]])
            y.append(label)
            w_naive.append(1.0 if click else 0.3)
            w_ips.append(ips_sample_weight(label, 1.0 if click else 0.3, pos))
    X, y = np.array(X), np.array(y)
    naive, _ = _fit_logistic(X, y, np.array(w_naive), np.zeros(2), l2=0.1)
    ips, _ = _fit_logistic(X, y, np.array(w_ips), np.zeros(2), l2=0.1)
    # relative credit given to the irrelevant, position-driving feature
    assert abs(ips[1] / ips[0]) < 0.5 * abs(naive[1] / naive[0])
    assert examination_propensity(0) == 1.0 and examination_propensity(9) < 0.25
