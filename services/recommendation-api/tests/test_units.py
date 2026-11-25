"""Pure helpers the Vesper integration relies on: age ceilings for kids profiles and the
prefetch model (when does this household watch, what will they start next)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import prefetch, ratings  # noqa: E402

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
