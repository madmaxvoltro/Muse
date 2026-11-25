import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content import build_payload, item_text, merge_metadata  # noqa: E402


def test_item_text_writes_people_as_phrases():
    text = item_text(
        {
            "title": "Arrival",
            "description": "A linguist works with the military.",
            "genres": ["Science Fiction", "Drama"],
            "directors": ["Denis Villeneuve"],
            "cast": ["Amy Adams", "Jeremy Renner", "Forest Whitaker", "Michael Stuhlbarg", "Extra"],
        }
    )
    assert text.startswith("Arrival")
    assert "Directed by Denis Villeneuve" in text
    assert "Starring Amy Adams, Jeremy Renner, Forest Whitaker, Michael Stuhlbarg" in text
    assert "Extra" not in text


def test_item_text_search_query_and_empty():
    assert item_text({"query": "space opera"}) == "space opera"
    assert item_text({}) == ""


def test_merge_keeps_known_fields():
    previous = {"title": "Arrival", "directors": ["Denis Villeneuve"], "official_rating": "PG-13"}
    merged = merge_metadata(previous, {"title": "Arrival", "directors": [], "description": "New"})
    assert merged["directors"] == ["Denis Villeneuve"]
    assert merged["official_rating"] == "PG-13"
    assert merged["description"] == "New"


def test_build_payload_keeps_first_seen():
    p1 = build_payload("jellyfin", "x", "movie", {"title": "A", "tmdb_id": "1"}, None, "2026-01-01T00:00:00+00:00")
    p2 = build_payload("jellyfin", "x", "movie", {"title": "A"}, p1, "2026-06-01T00:00:00+00:00")
    assert p2["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert p2["tmdb_id"] == "1"
    assert p2["item_type"] == "movie"


def test_merge_drops_event_bookkeeping():
    merged = merge_metadata(None, {"title": "A", "dedupe_key": "k", "via": "vesper", "value": 2})
    assert merged == {"title": "A"}


def test_collapse_events_dedupes_sources_and_states():
    from datetime import datetime, timezone

    from content import collapse_events

    t = lambda h: datetime(2026, 11, 1, h, tzinfo=timezone.utc)  # noqa: E731
    rows = [
        {"source": "jellyfin", "source_item_id": "a", "item_type": "movie", "action": "completed", "action_weight": 1.0, "timestamp": t(20)},
        {"source": "jellyfin", "source_item_id": "a", "item_type": "movie", "action": "completed", "action_weight": 1.0, "timestamp": t(21)},
        {"source": "jellyfin", "source_item_id": "a", "item_type": "movie", "action": "rated", "action_weight": 1.0, "timestamp": t(20)},
        {"source": "jellyfin", "source_item_id": "a", "item_type": "movie", "action": "rated", "action_weight": 2.0, "timestamp": t(22)},
        {"source": "jellyfin", "source_item_id": "b", "item_type": "movie", "action": "cataloged", "action_weight": 0.0, "timestamp": t(1)},
        {"source": "jellyfin", "source_item_id": "c", "item_type": "movie", "action": "completed", "action_weight": 1.0, "timestamp": t(1), "co_viewer": True},
    ]
    out = {(r["source_item_id"], round(w, 2)) for r, w in collapse_events(rows)}
    assert out == {("a", 1.0), ("a", 2.0), ("c", 0.6)}
    fav = [{"source": "m", "source_item_id": "x", "item_type": "track", "action": a, "action_weight": w, "timestamp": t(h)}
           for a, w, h in (("favorited", 1.5, 1), ("unfavorited", -1.5, 2), ("favorited", 1.5, 3))]
    assert [round(w, 2) for _, w in collapse_events(fav)] == [1.5]
    assert collapse_events(fav[:2]) == []
    # music: a replay hours later counts again, a duplicate report seconds later does not
    plays = [{"source": "mono", "source_item_id": "s", "item_type": "track", "action": "completed", "action_weight": 1.0,
              "timestamp": t(h).replace(second=sec)} for h, sec in ((10, 0), (10, 5), (14, 0))]
    assert len(collapse_events(plays)) == 2


def test_normalize_jellyfin_shape():
    from content import normalize

    md = normalize({"title": "Arrival", "people": [{"name": "Denis Villeneuve", "type": "Director"}, {"name": "Amy Adams", "type": "Actor"}],
                    "provider_ids": {"Tmdb": "329865"}, "taglines": ["Why are they here?"], "runtime_min": 116})
    assert md["directors"] == ["Denis Villeneuve"] and md["cast"] == ["Amy Adams"]
    assert md["tmdb_id"] == "329865" and md["tagline"] == "Why are they here?" and md["runtime_minutes"] == 116
    assert "Directed by Denis Villeneuve" in item_text({"title": "Arrival", "people": [{"name": "Denis Villeneuve", "type": "Director"}]})
