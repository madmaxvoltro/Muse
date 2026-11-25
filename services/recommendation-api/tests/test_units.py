"""Pure helpers the Vesper integration relies on: age ceilings for kids profiles and the
prefetch model (when does this household watch, what will they start next)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import prefetch, wrapped  # noqa: E402
from muse_schema import ratings  # noqa: E402

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


def test_prefetch_activity_and_ranking():
    tz = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    # watched every Friday at 20:00 local for 12 weeks
    ts = [datetime(2026, 11, 20, 19, 30, tzinfo=UTC) - timedelta(weeks=w) for w in range(12)]
    rates = prefetch.slot_rates(ts, tz, now)
    assert prefetch.active_probability(rates, now, 6, tz) > 0.7
    assert prefetch.active_probability(rates, now + timedelta(days=2), 3, tz) == 0.0
    events = [
        {"source": "jellyfin", "source_item_id": "m1", "item_type": "movie", "action": "played", "progress_pct": 40,
         "timestamp": now - timedelta(days=1), "metadata": {"title": "Dune"}},
        {"source": "jellyfin", "source_item_id": "e1", "item_type": "series_episode", "action": "completed", "progress_pct": 100,
         "timestamp": now - timedelta(hours=5), "metadata": {"series_id": "s1", "series_name": "Severance"}},
        {"source": "jellyfin", "source_item_id": "e2", "item_type": "series_episode", "action": "completed", "progress_pct": 100,
         "timestamp": now - timedelta(hours=4), "metadata": {"series_id": "s1", "series_name": "Severance"}},
        {"source": "jellyfin", "source_item_id": "old", "item_type": "movie", "action": "played", "progress_pct": 50,
         "timestamp": now - timedelta(days=60), "metadata": {}},
    ]
    rows = prefetch.aggregate(events, now)
    assert {r["source_item_id"] for r in rows} == {"m1", "e1", "e2"}  # older than 30 days drops out
    ranked = prefetch.rank_items(rows, now)
    assert {r["reason"] for r in ranked} == {"in_progress", "series_next"}
    assert next(r for r in ranked if r["reason"] == "series_next")["title"] == "Severance"


def test_wrapped_aggregate():
    tz = ZoneInfo("UTC")
    d = lambda day, h=21: datetime(2026, 3, day, h, tzinfo=UTC)  # noqa: E731
    md = {"title": "Arrival", "runtime_min": 116, "genres": ["Science Fiction"]}
    ep = lambda n: {"title": f"E{n}", "runtime_minutes": 50, "series_id": "s1", "series_name": "Severance", "genres": ["Drama"]}  # noqa: E731
    rows = [
        {"source": "jellyfin", "source_item_id": "m", "item_type": "movie", "action": "completed", "progress_pct": 100, "timestamp": d(1), "metadata": md},
        {"source": "jellyfin", "source_item_id": "m", "item_type": "movie", "action": "completed", "progress_pct": 100, "timestamp": d(1, 22), "metadata": md},
        {"source": "jellyfin", "source_item_id": "m", "item_type": "movie", "action": "rated", "progress_pct": None, "timestamp": d(1), "metadata": {**md, "value": 2}},
        *[{"source": "jellyfin", "source_item_id": f"e{n}", "item_type": "series_episode", "action": "completed", "progress_pct": 100,
           "timestamp": d(2, 20 + n % 4), "metadata": ep(n)} for n in range(3)],
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
