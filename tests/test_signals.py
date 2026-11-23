from datetime import timedelta

from muse_taste.signals import SignalEvent, aggregate

from synthetic import NOW


def ev(key, action, days_ago, progress=None, weight=0.0, item_type="movie", md=None, sub=None, source="jellyfin"):
    return SignalEvent(key, item_type, action, weight, NOW - timedelta(days=days_ago), source, progress_pct=progress, metadata=md or {}, sub_key=sub)


def test_polling_does_not_inflate_preference():
    """24 progress polls + completed must equal one completed watch, not 8x it."""
    many = [ev("a", "played", 10, progress=p, weight=0.3) for p in range(4, 100, 4)] + [ev("a", "completed", 10, 100, 1.0)]
    once = [ev("b", "completed", 10, 100, 1.0)]
    s = aggregate(many + once, now=NOW)
    assert abs(s["a"].preference - s["b"].preference) < 1e-9


def test_series_saturates():
    one = [ev("s1", "completed", 5, 100, 1.0, "series_episode", sub="e1")]
    forty = [ev("s40", "completed", 5, 100, 1.0, "series_episode", sub=f"e{i}") for i in range(40)]
    s = aggregate(one + forty, now=NOW)
    assert s["s40"].preference > s["s1"].preference
    assert s["s40"].preference < 1.5  # a binge is one strong signal, not forty
    assert s["s40"].item_type == "series"


def test_explicit_rating_overrides_viewing():
    evs = [ev("x", "completed", 30, 100, 1.0), ev("x", "rated", 29, weight=-1.0, md={"value": -1})]
    s = aggregate(evs, now=NOW)["x"]
    assert s.preference < 0 and s.explicit and s.seen


def test_cleared_rating_falls_back_to_implicit():
    evs = [ev("x", "completed", 30, 100, 1.0), ev("x", "rated", 29, weight=-1.0, md={"value": -1}), ev("x", "rated", 28, weight=0.0, md={"value": 0})]
    s = aggregate(evs, now=NOW)["x"]
    assert s.preference > 0 and not s.explicit


def test_early_abandon_is_negative_but_mood_cancels_it():
    ab = aggregate([ev("x", "played", 30, progress=10, weight=0.3)], now=NOW)["x"]
    assert ab.preference < 0 and ab.abandoned
    mood = aggregate([ev("x", "played", 30, progress=10, weight=0.3), ev("x", "abandon_reason", 2, md={"reason": "not_in_mood"}, source="muse")], now=NOW)["x"]
    assert mood.preference >= 0 and not mood.abandoned
    nfm = aggregate([ev("x", "played", 30, progress=10, weight=0.3), ev("x", "abandon_reason", 2, md={"reason": "not_for_me"}, source="muse")], now=NOW)["x"]
    assert nfm.preference < ab.preference and nfm.explicit


def test_recent_partial_is_in_progress_not_abandoned():
    s = aggregate([ev("x", "played", 2, progress=30, weight=0.3)], now=NOW)["x"]
    assert s.in_progress and not s.abandoned and s.preference > 0


def test_half_life_is_a_real_half_life():
    from muse_taste import signals as sg

    s = aggregate([ev("x", "completed", sg.IMPLICIT_HALF_LIFE_DAYS, 100, 1.0)], now=NOW)["x"]
    assert abs(s.decay(NOW) - 0.5) < 1e-6


def test_explicit_never_fully_decays():
    s = aggregate([ev("x", "rated", 5000, weight=2.0, md={"value": 2})], now=NOW)["x"]
    assert s.decay(NOW) >= 0.35


def test_watchlist_is_intent_not_seen():
    s = aggregate([ev("x", "added_watchlist", 3, weight=0.4)], now=NOW)["x"]
    assert s.intent > 0 and not s.seen


def test_disabled_source_and_weights():
    evs = [ev("x", "completed", 3, 100, 1.0, source="netflix"), ev("y", "completed", 3, 100, 1.0, source="jellyfin")]
    s = aggregate(evs, now=NOW, disabled_sources=["netflix"], source_weights={"jellyfin": 0.5})
    assert "x" not in s and abs(s["y"].preference - 0.5) < 1e-9


def test_music_skips():
    evs = [ev("t", "skipped", 3, item_type="track") for _ in range(4)]
    s = aggregate(evs, now=NOW)["t"]
    assert s.preference < 0
    loved = aggregate([ev("t2", "completed", 3, 100, 1.0, item_type="track") for _ in range(10)], now=NOW)["t2"]
    assert loved.preference > 0.5
