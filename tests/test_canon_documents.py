from muse_taste.canon import canon_key, domain_of, norm_artist, norm_track_title, parent_canon_key
from muse_taste.documents import build_document, build_tags, merged_fields


def test_canon_prefers_tmdb_then_imdb():
    assert canon_key("jellyfin", "abc", "movie", {"provider_ids": {"Tmdb": "603", "Imdb": "tt0133093"}}) == "tmdb:movie:603"
    assert canon_key("letterboxd", "x", "movie", {"external_ids": {"imdb": "tt0133093"}}) == "imdb:tt0133093"
    assert canon_key("jellyfin", "abc", "series", {"provider_ids": {"Tmdb": "1399"}}) == "tmdb:tv:1399"
    assert canon_key("netflix", "Some Film", "movie", {"title": "Some Film"}) == "netflix:Some Film"


def test_tracks_match_across_services():
    a = canon_key("spotify", "spotify:track:1", "track", {"title": "Halo (feat. Someone) - 2019 Remaster", "artist": "Beyoncé"})
    b = canon_key("youtube", "vid", "track", {"title": "Halo", "artist": "Beyoncé - Topic"})
    assert a == b == "track:beyonce|halo"
    assert norm_artist("The Beatles") == "beatles"
    assert norm_track_title("Song - Radio Edit") == "song"


def test_episode_rolls_up_to_series():
    assert parent_canon_key("jellyfin", "series_episode", {"series_provider_ids": {"Tmdb": "1399"}}) == "tmdb:tv:1399"
    assert parent_canon_key("jellyfin", "series_episode", {"series_id": "S1"}) == "jellyfin:S1"
    assert parent_canon_key("netflix", "series_episode", {"series_title": "Dark"}) == "title:tv:dark"
    assert parent_canon_key("jellyfin", "movie", {}) is None
    assert domain_of("series") == domain_of("movie") == "video"


def test_document_and_tags():
    f = merged_fields(
        "movie",
        {"title": "Arrival", "year": 2016, "genres": ["Drama"], "description": "short"},
        {"keywords": ["first contact", "linguistics"], "directors": ["Denis Villeneuve"], "cast": ["Amy Adams"], "overview": "A linguist is recruited...", "collection": None, "original_language": "en"},
        {"tone": ["cerebral", "melancholic"]},
    )
    doc = build_document(f)
    assert doc.startswith("Arrival (2016). Film.")
    assert "Keywords: first contact, linguistics" in doc and "Tone: cerebral, melancholic" in doc
    assert "A linguist is recruited" in doc  # longer overview wins
    tags = build_tags(f)
    assert "d:denis villeneuve" in tags and "k:first contact" in tags and "decade:2010s" in tags and "t:cerebral" in tags
