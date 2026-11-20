"""Navidrome and Audiobookshelf adapters: the parts that turn API data into events."""

from __future__ import annotations

import importlib.util
import sys

from conftest import ROOT
from muse_schema import Event


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ND = load("nd_adapter", ROOT / "adapters" / "navidrome" / "main.py")
ABS = load("abs_adapter", ROOT / "adapters" / "audiobookshelf" / "main.py")


def entry(id_, user="max", minutes_ago=0, duration=200):
    return {"id": id_, "username": user, "minutesAgo": minutes_ago, "duration": duration, "title": f"Song {id_}", "artist": "Racoon",
            "album": "Liverpool Rain", "genre": "Dutch Rock", "playerName": "Feishin"}


def test_now_playing_completed_and_skipped():
    np = ND.NowPlaying()
    out = np.update([entry("a")], now=1000.0)
    assert [(e["action"], e["source_item_id"]) for e in out] == [("played", "a")]
    assert out[0]["device_context"] == {"device": "Feishin"} and out[0]["metadata"]["title"] == "Racoon - Song a"
    assert np.update([entry("a")], now=1100.0) == []  # still playing
    out = np.update([entry("b")], now=1195.0)  # a ran 195 of 200 s
    assert [(e["action"], e["source_item_id"]) for e in out] == [("completed", "a"), ("played", "b")]
    out = np.update([entry("c")], now=1215.0)  # b ran 20 s
    assert out[0]["action"] == "skipped" and out[0]["progress_pct"] == 10.0
    out = np.update([], now=1320.0)  # c ran 105/200: neither clearly finished nor skipped
    assert out == []
    for e in [*np.update([entry("d", user="kim")], now=2000.0)]:
        Event(**e)


def test_audiobookshelf_progress():
    user = {"id": "u1", "mediaProgress": [
        {"libraryItemId": "b1", "isFinished": True, "progress": 1.0, "lastUpdate": 1_700_000_000_000},
        {"libraryItemId": "b2", "isFinished": False, "progress": 0.4, "lastUpdate": 1_700_000_000_000},
        {"libraryItemId": "b3", "isFinished": False, "progress": 0.01},
        {"libraryItemId": "p1", "episodeId": "e1", "progress": 0.5},
    ]}
    books = {"b1": ABS.book_md({"media": {"metadata": {"title": "Dune", "authorName": "Frank Herbert", "genres": ["Sci-Fi"]}, "duration": 76000}})}
    ev = ABS.progress_events(user, books, "2026-11-20")
    assert [(e["action"], e["source_item_id"]) for e in ev] == [("completed", "b1"), ("played", "b2")]
    assert ev[0]["metadata"]["author"] == "Frank Herbert" and ev[1]["progress_pct"] == 40.0
    for e in ev:
        Event(**e)
