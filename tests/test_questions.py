from datetime import timedelta

from muse_taste.evolution import adjustment, analyze
from muse_taste.questions import interest_questions, pair_question
from muse_taste.signals import SignalEvent

from synthetic import NOW, world
from test_evolution import _by_genre, _profile, done, shift_history


def test_interest_questions_follow_evolution_states():
    vectors, genre_of, _, tags = world()
    evs = shift_history(genre_of)  # crime -> animation
    prof = _profile(vectors, tags, evs)
    prof.evolution = analyze(prof, evs, vectors, now=NOW)
    qs = {q.payload["subkind"]: q for q in interest_questions(prof, {k: k for k in vectors})}
    assert "emerging" in qs and genre_of[qs["emerging"].payload["interest"]["anchor_key"]] == "anim"
    assert "dormant" not in qs or genre_of[qs["dormant"].payload["interest"]["anchor_key"]] == "crime"
    q = qs["emerging"]
    assert q.dedupe_key.startswith("interest:emerging:") and q.ttl_days > 0 and {a["response"] for a in q.payload["answers"]} == {"more", "keep", "less"}


def test_pair_dedupe_is_order_independent():
    a, b = {"canon_key": "x"}, {"canon_key": "y"}
    assert pair_question("video", a, b).dedupe_key == pair_question("video", b, a).dedupe_key


def _feedback(anchor, response, days_ago, members):
    return SignalEvent(anchor, "movie", "interest_feedback", 0.0, NOW - timedelta(days=days_ago), "muse",
                       metadata={"response": response, "members": members, "canon_key": anchor})


def test_answers_steer_evolution_and_unanswered_changes_nothing():
    vectors, genre_of, _, tags = world()
    g = _by_genre(genre_of)
    base = [done(k, 200 - i * 4) for i, k in enumerate(g["crime"][:25])] + [done(k, 190 - i * 4) for i, k in enumerate(g["anim"][:25])]
    prof = _profile(vectors, tags, base)
    anim = next(i for i in prof.interests if genre_of[i.medoid_key] == "anim")
    crime = next(i for i in prof.interests if genre_of[i.medoid_key] == "crime")

    plain = analyze(prof, base, vectors, now=NOW)
    more = analyze(prof, base + [_feedback(anim.medoid_key, "more", 3, anim.members)], vectors, now=NOW)
    assert plain["interests"][str(anim.id)]["steer"] == 0.0  # no answer, no effect
    assert more["interests"][str(anim.id)]["steer"] > 0.9
    prof.evolution = more
    adj = adjustment(prof, at=NOW)
    assert adj.factor[anim.id] > adj.factor[crime.id]

    # an old answer fades
    old = analyze(prof, base + [_feedback(anim.medoid_key, "more", 240, anim.members)], vectors, now=NOW)
    assert old["interests"][str(anim.id)]["steer"] < 0.1


def test_moved_on_blocks_rediscovery():
    vectors, genre_of, _, tags = world()
    g = _by_genre(genre_of)
    evs = [done(k, 700 - i * 4) for i, k in enumerate(g["docu"][:40])] + [done(k, 100 - i * 3) for i, k in enumerate(g["crime"][:30])]
    prof = _profile(vectors, tags, evs)
    docu = next(i for i in prof.interests if genre_of[i.medoid_key] == "docu")
    before = analyze(prof, evs, vectors, now=NOW)["interests"][str(docu.id)]["rediscovery"]
    after = analyze(prof, evs + [_feedback(docu.medoid_key, "moved_on", 2, docu.members)], vectors, now=NOW)["interests"][str(docu.id)]["rediscovery"]
    assert before > 0.3 and after < 0.05
