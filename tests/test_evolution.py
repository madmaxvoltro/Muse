from datetime import datetime, timedelta, timezone

import numpy as np

from muse_taste.evaluate import evaluate
from muse_taste.evolution import adjustment, analyze, apply_importance, stretch_score, touches
from muse_taste.profile import build_profile
from muse_taste.ranking import CandidateInput, explain, score_candidates
from muse_taste.signals import SignalEvent, aggregate

from synthetic import NOW, world


def _by_genre(genre_of):
    out = {}
    for k, g in genre_of.items():
        out.setdefault(g, []).append(k)
    return out


def done(key, days_ago, hour=20, weekday_shift=0):
    ts = (NOW - timedelta(days=days_ago)).replace(hour=hour)
    return SignalEvent(key, "movie", "completed", 1.0, ts, "jellyfin", progress_pct=100)


def shift_history(genre_of, seed=0):
    """Crime for a year, then switched to animation over the last two months."""
    rng = np.random.default_rng(seed)
    g = _by_genre(genre_of)
    crime, anim = list(g["crime"]), list(g["anim"])
    rng.shuffle(crime)
    rng.shuffle(anim)
    evs = [done(k, 420 - i * 5) for i, k in enumerate(crime[:50])]  # 420..175 days ago
    evs += [done(k, 70 - i * 3) for i, k in enumerate(anim[:22])]  # last 70 days
    return sorted(evs, key=lambda e: e.timestamp)


def _profile(vectors, tags, evs, now=NOW):
    sig = aggregate(evs, now=now)
    return build_profile("u", "video", sig, vectors, tags, now=now)


def test_lifecycle_emerging_and_fading():
    vectors, genre_of, _, tags = world()
    evs = shift_history(genre_of)
    prof = _profile(vectors, tags, evs)
    ev = analyze(prof, evs, vectors, now=NOW)
    states = {genre_of[prof.interests[int(k)].medoid_key]: v for k, v in ev["interests"].items()}
    assert states["anim"]["state"] in ("emerging", "growing") and states["anim"]["trend"] > 0.3
    assert states["anim"]["satiation"] == 0.0  # diving into something new isn't boredom
    assert states["crime"]["state"] in ("fading", "dormant") and states["crime"]["trend"] < -0.3
    kinds = {m["kind"] for m in ev["milestones"]}
    assert "new_interest" in kinds
    months = [r["month"] for r in ev["timeline"]]
    assert months == sorted(months) and len(months) >= 10
    assert max(ev["timeline"], key=lambda r: r["drift"])["month"] == next(m["month"] for m in ev["milestones"] if m["kind"] == "new_interest")


def test_evolution_helps_after_a_taste_shift():
    vectors, genre_of, catalog, tags = world(per_genre=60)
    users = {f"u{s}": shift_history(genre_of, seed=s) for s in range(8)}
    res = evaluate(users, catalog, vectors, tags, strategies=("full+rerank", "full+evolution"))
    print("\n" + res.table())
    m = res.per_strategy
    assert m["full+evolution"]["recall@10"] > m["full+rerank"]["recall@10"]
    assert m["full+evolution"]["ndcg@10"] > m["full+rerank"]["ndcg@10"]


def test_satiation_needs_binge_and_fatigue():
    vectors, genre_of, _, tags = world()
    g = _by_genre(genre_of)
    base = [done(k, 300 - i * 10) for i, k in enumerate(g["crime"][:20])] + [done(k, 290 - i * 10) for i, k in enumerate(g["anim"][:20])]
    binge = [done(k, 12 - i * 0.7) for i, k in enumerate(g["crime"][20:36])]
    tired = [SignalEvent(k, "movie", "played", 0.3, NOW - timedelta(days=2), "jellyfin", progress_pct=8) for k in g["crime"][36:40]]
    tired += [SignalEvent(k, "movie", "abandon_reason", 0.0, NOW - timedelta(days=1), "muse", metadata={"reason": "not_for_me"}) for k in g["crime"][36:40]]
    for evs, expect_high in ((base + binge, False), (base + binge + tired, True)):
        prof = _profile(vectors, tags, evs)
        ev = analyze(prof, evs, vectors, now=NOW)
        sat = max(v["satiation"] for k, v in ev["interests"].items() if genre_of[prof.interests[int(k)].medoid_key] == "crime")
        other = max(v["satiation"] for k, v in ev["interests"].items() if genre_of[prof.interests[int(k)].medoid_key] == "anim")
        assert other < 0.1
        assert (sat > 0.5) if expect_high else (sat <= 0.4), sat


def test_rediscovery_of_a_dormant_love():
    vectors, genre_of, _, tags = world()
    g = _by_genre(genre_of)
    evs = [done(k, 700 - i * 4) for i, k in enumerate(g["docu"][:40])] + [done(k, 100 - i * 3) for i, k in enumerate(g["crime"][:30])]
    prof = _profile(vectors, tags, evs)
    ev = analyze(prof, evs, vectors, now=NOW)
    docu = [v for k, v in ev["interests"].items() if genre_of[prof.interests[int(k)].medoid_key] == "docu"]
    assert docu and docu[0]["state"] == "dormant" and docu[0]["rediscovery"] > 0.3


def test_rhythms_and_context_adjustment():
    vectors, genre_of, _, tags = world()
    g = _by_genre(genre_of)
    # animation on Saturday/Sunday mornings, crime on weekday evenings
    evs = []
    for i, k in enumerate(g["anim"][:20]):
        d = NOW - timedelta(days=7 * (i + 1))
        d = d - timedelta(days=(d.weekday() - 5) % 7)  # Saturday
        evs.append(SignalEvent(k, "movie", "completed", 1.0, d.replace(hour=9), "jellyfin", progress_pct=100))
    for i, k in enumerate(g["crime"][:20]):
        d = NOW - timedelta(days=7 * (i + 1))
        d = d - timedelta(days=(d.weekday() - 2) % 7)  # Wednesday
        evs.append(SignalEvent(k, "movie", "completed", 1.0, d.replace(hour=21), "jellyfin", progress_pct=100))
    evs.sort(key=lambda e: e.timestamp)
    prof = _profile(vectors, tags, evs)
    prof.evolution = analyze(prof, evs, vectors, now=NOW)
    anim_k = next(i.id for i in prof.interests if genre_of[i.medoid_key] == "anim")
    ctx = prof.evolution["interests"][str(anim_k)]["context"]
    assert ctx["weekpart"]["weekend"] > 1.3 and ctx["daypart"]["morning"] > 1.3
    saturday_morning = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)
    adj = adjustment(prof, at=saturday_morning)
    assert adj.context[anim_k] > 0.3 and adj.factor[anim_k] > adj.factor[1 - anim_k]
    off = adjustment(prof, at=saturday_morning, strength=0.0)
    assert off is None


def test_stretch_band():
    sims = np.array([[0.8, 0.1], [0.5, 0.1], [0.05, 0.0]])
    radii = np.array([0.6, 0.6])
    s = stretch_score(sims, radii, np.array([0.7, 0.3]), band=0.2)
    assert s[0] == 0.0  # inside the interest
    assert s[1] == 1.0  # middle of the band just beyond the edge
    assert s[2] == 0.0  # too far


def test_reasons_and_explore_prefers_the_frontier():
    from muse_taste.rerank import select

    vectors, genre_of, _, tags = world()
    evs = shift_history(genre_of)
    prof = _profile(vectors, tags, evs)
    prof.evolution = analyze(prof, evs, vectors, now=NOW)
    adj = adjustment(prof, at=NOW)
    apply_importance(prof, adj)
    seen = {e.key for e in evs}
    cands = [CandidateInput(key=k, vector=vectors[k], tags=tags[k]) for k in vectors if k not in seen]
    scored = sorted(score_candidates(prof, cands, anchor_vectors=vectors, evolution=adj), key=lambda s: -s.score)
    titles = {k: k for k in vectors}
    anim_top = next(s for s in scored if genre_of[s.key] == "anim")
    assert any("lately" in r for r in explain(anim_top, prof, titles, max_reasons=5)), explain(anim_top, prof, titles, 5)
    out = select(scored[:300], prof, limit=20, explore_ratio=0.2, seed=3)
    explore = [s for s in out if s.explore]
    assert explore and np.mean([s.features["stretch"] for s in explore]) >= np.mean([s.features["stretch"] for s in scored[:300]])


def test_touches_cap_daily_binges():
    evs = [SignalEvent("s", "series_episode", "completed", 1.0, NOW - timedelta(hours=h), "jellyfin", sub_key=f"e{h}") for h in range(10)]
    t = touches(evs)
    assert len(t) <= 2 and all(x.weight <= 2.5 for x in t)
