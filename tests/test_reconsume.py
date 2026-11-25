from datetime import timedelta

from muse_taste.reconsume import activation, candidates
from muse_taste.signals import SignalEvent, aggregate

from synthetic import NOW


def ev(key, days, item_type="movie", action="completed", weight=1.0, md=None):
    return SignalEvent(key, item_type, action, weight, NOW - timedelta(days=days), "jellyfin", progress_pct=100, metadata=md or {})


def test_activation_recency_and_frequency():
    assert activation([NOW - timedelta(days=2)], NOW) > activation([NOW - timedelta(days=200)], NOW)
    many = [NOW - timedelta(days=d) for d in (10, 40, 90)]
    assert activation(many, NOW) > activation(many[:1], NOW)


def test_rewatch_prefers_loved_faded_titles():
    evs = [
        ev("loved_old", 700), ev("loved_old", 699, action="rated", weight=2.0, md={"value": 2}),
        ev("loved_recent", 20), ev("loved_recent", 19, action="rated", weight=2.0, md={"value": 2}),
        ev("meh_old", 700),  # watched, never loved
        ev("comfort", 900), ev("comfort", 500), ev("comfort", 300), ev("comfort", 299, action="favorited", weight=1.5),
    ]
    sig = aggregate(evs, now=NOW)
    keys = [c["key"] for c in candidates("video", sig, evs, now=NOW)]
    assert "loved_recent" not in keys and "meh_old" not in keys
    assert keys[0] == "comfort" and "loved_old" in keys  # rewatched before + loved -> strongest pull


def test_relisten_prefers_on_repeat():
    evs = [ev("hit", d, item_type="track") for d in (1, 2, 4, 6)] + [ev("old", 400, item_type="track")]
    sig = aggregate(evs, now=NOW)
    c = candidates("music", sig, evs, now=NOW)
    assert c[0]["key"] == "hit" and c[0]["why"] == "on repeat lately"
