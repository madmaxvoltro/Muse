import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, history, prefetch, ratings, wrapped  # noqa: E402
from app.identity import new_code, normalise_code  # noqa: E402
from app.migrate import pending  # noqa: E402
from app.reasons import Anchor, reasons_for, top_genres  # noqa: E402

UTC = timezone.utc


def test_ratings_normalise_common_systems():
    assert ratings.min_age("PG-13") == 13
    assert ratings.min_age("NL-12") == 12
    assert ratings.min_age("FSK 16") == 16
    assert ratings.min_age("DE-FSK 6") == 6
    assert ratings.min_age("TV-MA") == 17
    assert ratings.min_age("12A") == 12
    assert ratings.min_age("AL") == 0
    assert ratings.min_age("Not Rated") is None
    assert ratings.within("PG", 9) and not ratings.within("PG-13", 9)
    assert not ratings.within(None, 9) and ratings.within(None, 9, allow_unrated=True)
    assert ratings.within("R", None)


def test_auth_scopes():
    read, ctl = {"r": "vesper"}, {"c": "vesper"}
    assert auth.allowed("GET", "/healthz", None, read, ctl)
    assert auth.allowed("GET", "/recommendations/x", "r", read, ctl)
    assert not auth.allowed("PUT", "/users/x/overlay", "r", read, ctl)
    assert auth.allowed("PUT", "/users/x/overlay", "c", read, ctl)
    assert auth.allowed("POST", "/search/semantic", "r", read, ctl)
    # no control keys configured: read keys may write (Jellyfin plugin whitelist hook)
    assert auth.allowed("POST", "/soon-gone/1/whitelist", "r", read, {})
    assert auth.allowed("GET", "/anything", None, {}, {})
    assert not auth.allowed("GET", "/recommendations/x", None, read, ctl)


def test_history_strength_and_kind():
    now = datetime.now(UTC)
    items = history.aggregate([
        {"source": "jellyfin", "source_item_id": "a", "item_type": "movie", "completions": 2, "rating": "2", "last_ts": now},
        {"source": "jellyfin", "source_item_id": "b", "item_type": "movie", "rating": "-1"},
        {"source": "jellyfin", "source_item_id": "c", "item_type": "movie", "played": True, "max_progress": 40, "co_viewed": True},
        {"source": "jellyfin", "source_item_id": "d", "item_type": "movie", "impressions": 5, "together": [["u2"], ["u3"]]},
    ])
    assert items["jellyfin:a"].strength == 2.25 and items["jellyfin:a"].kind == "loved"
    assert items["jellyfin:b"].strength == -1 and items["jellyfin:b"].disliked
    assert abs(items["jellyfin:c"].strength - 0.21) < 1e-9
    assert not items["jellyfin:d"].seen and items["jellyfin:d"].together == {"u2", "u3"}


def test_reasons_anchor_director_and_explore():
    v = np.array([1.0, 0, 0])
    anchors = [
        Anchor("jellyfin:a", "a", "Arrival", "finished", 1.0, 1.0, [0.95, 0.1, 0], directors=["Denis Villeneuve"], genres=["Science Fiction"]),
        Anchor("jellyfin:b", "b", "Notting Hill", "loved", 2.0, 0.2, [0, 1.0, 0], genres=["Romance"]),
    ]
    rs = reasons_for(v, {"directors": ["Denis Villeneuve"], "genres": ["Science Fiction"], "community_rating": 8.1}, anchors, top_genres(anchors), explore=True)
    codes = [r["code"] for r in rs]
    assert codes[0] == "because_finished" and rs[0]["anchor_title"] == "Arrival"
    assert "same_director" in codes
    assert len(rs) <= 4
    group = reasons_for(v, {}, [Anchor("jellyfin:a", "a", "Arrival", "finished", 1.0, 1.0, [1, 0, 0], together={"u2"})], [], group_members={"u2"})
    assert group[0]["code"] == "watched_together"


def test_prefetch_activity_and_ranking():
    tz = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    # watched every Friday at 20:00 local for 12 weeks
    ts = [datetime(2026, 11, 20, 19, 30, tzinfo=UTC) - timedelta(weeks=w) for w in range(12)]
    rates = prefetch.slot_rates(ts, tz, now)
    assert prefetch.active_probability(rates, now, 6, tz) > 0.7
    assert prefetch.active_probability(rates, now + timedelta(days=2), 3, tz) == 0.0
    rows = [
        {"source": "jellyfin", "source_item_id": "m1", "item_type": "movie", "progress": 40, "ts": now - timedelta(days=1), "completed": False, "title": "Dune"},
        {"source": "jellyfin", "source_item_id": "e1", "item_type": "series_episode", "progress": 100, "ts": now - timedelta(hours=5), "completed": True, "series_id": "s1", "series_name": "Severance"},
        {"source": "jellyfin", "source_item_id": "e2", "item_type": "series_episode", "progress": 100, "ts": now - timedelta(hours=4), "completed": True, "series_id": "s1", "series_name": "Severance"},
    ]
    ranked = prefetch.rank_items(rows, now)
    assert {r["reason"] for r in ranked} == {"in_progress", "series_next"}
    assert ranked[0]["source_item_id"] in ("m1", "s1")


def test_wrapped_aggregate():
    tz = ZoneInfo("UTC")
    d = lambda day, h=21: datetime(2026, 3, day, h, tzinfo=UTC)  # noqa: E731
    md = {"title": "Arrival", "runtime_minutes": 116, "genres": ["Science Fiction"]}
    ep = lambda n: {"title": f"E{n}", "runtime_minutes": 50, "series_id": "s1", "series_name": "Severance", "genres": ["Drama"]}  # noqa: E731
    rows = [
        {"source": "jellyfin", "source_item_id": "m", "item_type": "movie", "action": "completed", "progress_pct": 100, "timestamp": d(1), "metadata": md},
        {"source": "jellyfin", "source_item_id": "m", "item_type": "movie", "action": "completed", "progress_pct": 100, "timestamp": d(1, 22), "metadata": md},
        {"source": "jellyfin", "source_item_id": "m", "item_type": "movie", "action": "rated", "progress_pct": None, "timestamp": d(1), "metadata": {**md, "value": 2}},
        *[{"source": "jellyfin", "source_item_id": f"e{n}", "item_type": "series_episode", "action": "completed", "progress_pct": 100, "timestamp": d(2, 20 + n % 4), "metadata": ep(n)} for n in range(3)],
        {"source": "jellyfin", "source_item_id": "e9", "item_type": "series_episode", "action": "completed", "progress_pct": 100, "timestamp": d(3), "metadata": ep(9)},
    ]
    w = wrapped.aggregate(rows, tz)
    assert w["minutes"] == 116 + 4 * 50  # same film twice in one day counts once
    assert w["films"] == 1 and w["episodes"] == 4 and w["series"] == 1
    assert w["top_films"][0]["title"] == "Arrival" and w["top_films"][0]["rating"] == 2
    assert w["top_series"][0]["best_day_episodes"] == 3
    assert w["longest_streak_days"] == 3 and w["busiest_month"] == 3
    kids = wrapped.aggregate(rows, tz, kids=True)
    assert "minutes" not in kids and kids["films"] == 1


def test_codes_and_migrations():
    c = new_code()
    assert len(c) == 7 and c[3] == "-" and not set(c) & set("01OI")
    assert normalise_code(" abc 123 ") == "ABC-123"
    assert pending(["002_b.sql", "001_a.sql", "README.md", "003_c.sql"], {"001_a.sql"}) == ["002_b.sql", "003_c.sql"]
