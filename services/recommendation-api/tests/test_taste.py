import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.taste import affinity, blend, facets, group_affinity, match_percent, mmr, unit  # noqa: E402


def _cluster(center, n, rng):
    return [unit(center + rng.normal(0, 0.05, size=center.shape)) for _ in range(n)]


def test_facets_split_two_tastes():
    rng = np.random.default_rng(1)
    a, b = np.zeros(16), np.zeros(16)
    a[0], b[1] = 1, 1
    vecs = _cluster(a, 8, rng) + _cluster(b, 4, rng)
    fs = facets(vecs, [1.0] * 12, k_max=2)
    assert len(fs) == 2
    assert fs[0].weight > fs[1].weight
    # an item like the minority taste still scores high: the mean vector would not
    assert affinity(b, fs) > 0.7
    mean = unit(np.mean(vecs, axis=0))
    assert float(np.dot(unit(b), mean)) < affinity(b, fs)


def test_single_facet_for_little_history():
    fs = facets([np.ones(4)], [1.0])
    assert len(fs) == 1 and fs[0].weight == 1.0
    assert facets([], []) == []


def test_group_affinity_least_misery():
    assert group_affinity([0.9, 0.1]) < group_affinity([0.5, 0.5])


def test_blend_and_mmr():
    v = blend([(np.array([1.0, 0]), 0.5), (np.array([0, 1.0]), 0.5)])
    assert abs(float(np.linalg.norm(v)) - 1) < 1e-6
    vecs = [np.array([1.0, 0]), np.array([0.99, 0.01]), np.array([0, 1.0])]
    picked = mmr([0, 1, 2], vecs, [1.0, 0.99, 0.6], limit=2)
    assert picked == [0, 2]


def test_match_percent_range():
    assert match_percent(-1) == 1 and match_percent(2) == 99
    assert 85 <= match_percent(0.55) <= 95
