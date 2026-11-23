import numpy as np

from muse_taste.profile import TasteProfile, build_profile, estimate_baseline
from muse_taste.ranking import CandidateInput, explain, score_candidates
from muse_taste.rerank import select
from muse_taste.signals import aggregate

from synthetic import NOW, user_history, world


def _setup():
    vectors, genre_of, catalog, tags = world()
    events = user_history(genre_of)
    sig = aggregate(events, now=NOW)
    prof = build_profile("u1", "video", sig, vectors, tags, now=NOW)
    return vectors, genre_of, catalog, tags, sig, prof


def test_multi_interest_finds_both_loves():
    vectors, genre_of, _, _, _, prof = _setup()
    assert len(prof.interests) >= 2
    top_genres = [genre_of[i.medoid_key] for i in prof.interests[:3]]
    assert "crime" in top_genres and "anim" in top_genres
    share = {}
    for i in prof.interests:  # a big interest may legitimately split into sub-interests
        share[genre_of[i.medoid_key]] = share.get(genre_of[i.medoid_key], 0.0) + i.importance
    assert share["crime"] > share["anim"]  # watched 60/40
    # the 40-episode binge must not become a dominant interest
    assert share.get("docu", 0.0) < 0.1


def test_negatives_are_prototypes_not_averaged():
    _, genre_of, _, _, _, prof = _setup()
    assert prof.negatives and all(genre_of[n.key] == "horror" for n in prof.negatives)


def test_profile_roundtrip_json():
    *_, prof = _setup()
    again = TasteProfile.from_json(prof.to_json())
    assert len(again.interests) == len(prof.interests) and again.baseline == prof.baseline
    assert again.interests[0].medoid_key == prof.interests[0].medoid_key


def _score(prof, vectors, tags, genre_of, keys):
    cands = [CandidateInput(key=k, vector=vectors[k], tags=tags[k]) for k in keys]
    return score_candidates(prof, cands, anchor_vectors=vectors)


def test_ranking_prefers_loved_genres_and_pushes_dislikes_down():
    vectors, genre_of, _, tags, sig, prof = _setup()
    unseen = [k for k in vectors if k not in sig]
    scored = sorted(_score(prof, vectors, tags, genre_of, unseen), key=lambda s: -s.score)
    top20 = [genre_of[s.key] for s in scored[:20]]
    assert sum(g in ("crime", "anim") for g in top20) >= 18
    horror_rank = np.mean([i for i, s in enumerate(scored) if genre_of[s.key] == "horror"])
    assert horror_rank > len(scored) * 0.6


def test_uncertainty_is_lower_near_evidence():
    vectors, genre_of, _, tags, sig, prof = _setup()
    unseen = [k for k in vectors if k not in sig]
    scored = _score(prof, vectors, tags, genre_of, unseen)
    crime_sigma = np.mean([s.sigma for s in scored if genre_of[s.key] == "crime"])
    romcom_sigma = np.mean([s.sigma for s in scored if genre_of[s.key] == "romcom"])
    assert crime_sigma < romcom_sigma


def test_rerank_is_calibrated_and_explores():
    vectors, genre_of, _, tags, sig, prof = _setup()
    unseen = [k for k in vectors if k not in sig]
    scored = sorted(_score(prof, vectors, tags, genre_of, unseen), key=lambda s: -s.score)[:200]
    out = select(scored, prof, limit=20, explore_ratio=0.15, seed=42)
    assert len(out) == 20 and len({s.key for s in out}) == 20
    exploit = [s for s in out if not s.explore]
    g = [genre_of[s.key] for s in exploit]
    crime, anim = g.count("crime"), g.count("anim")
    assert crime >= 5 and anim >= 4, (crime, anim)  # both interests represented
    assert any(s.explore for s in out)
    assert out[-1].explore is False or len([s for s in out if s.explore]) > 1  # interleaved, not all at the end


def test_collection_keeps_earliest_unseen_part():
    vectors, genre_of, _, tags, sig, prof = _setup()
    keys = [k for k in vectors if genre_of[k] == "crime" and k not in sig][:3]
    cands = [CandidateInput(key=k, vector=vectors[k], tags=tags[k], payload={"collection_id": 9, "year": 2010 - i}) for i, k in enumerate(keys)]
    scored = score_candidates(prof, cands, anchor_vectors=vectors)
    out = select(scored, prof, limit=3, explore_ratio=0)
    assert len(out) == 1 and out[0].payload["year"] == 2008


def test_explanations_are_human():
    vectors, genre_of, _, tags, sig, prof = _setup()
    titles = {k: k.split(":")[-1] for k in vectors}
    unseen = [k for k in vectors if k not in sig and genre_of[k] == "crime"][:5]
    s = sorted(_score(prof, vectors, tags, genre_of, unseen), key=lambda s: -s.score)[0]
    reasons = explain(s, prof, titles)
    assert reasons and all(isinstance(r, str) and r for r in reasons)


def test_baseline_estimate_reasonable():
    rng = np.random.default_rng(0)
    v = rng.standard_normal((200, 32))
    assert estimate_baseline(v) < 0.1
