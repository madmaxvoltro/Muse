"""A small synthetic world with known ground truth, shared by the tests.

Six latent genres; a user who loves two of them (60/40), dislikes one, and once binged a
40-episode show in a genre they're lukewarm about (the case that broke the old pipeline).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from muse_taste.signals import SignalEvent

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
GENRES = ["crime", "anim", "horror", "docu", "romcom", "scifi"]


def world(seed: int = 7, d: int = 48, per_genre: int = 120, noise: float = 0.45, decoy: bool = False):
    """decoy=True adds a seventh genre sitting exactly between the user's two loves, which
    they never watch: the region a single averaged taste vector points at."""
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((len(GENRES), d))
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    genres = list(GENRES)
    if decoy:
        mid = centers[0] + centers[1]
        centers = np.vstack([centers, mid / np.linalg.norm(mid)])
        genres.append("blend")
    vectors, genre_of, catalog, tags = {}, {}, {}, {}
    for gi, g in enumerate(genres):
        for j in range(per_genre):
            v = centers[gi] + noise * rng.standard_normal(d) / np.sqrt(d) * 3
            key = f"tmdb:movie:{g}{j}"
            vectors[key] = v / np.linalg.norm(v)
            genre_of[key] = g
            director = f"d:{g}-director-{j % 6}"
            tags[key] = [f"g:{g}", director, f"k:{g}-kw-{j % 10}"]
            catalog[key] = {"item_type": "movie", "domain": "video", "vote_average": 6.0 + (j % 5) * 0.5, "vote_count": 500, "collection_id": None, "year": 2000 + j % 25}
    return vectors, genre_of, catalog, tags


def user_history(genre_of: dict[str, str], seed: int = 3) -> list[SignalEvent]:
    rng = np.random.default_rng(seed)
    by_genre: dict[str, list[str]] = {}
    for k, g in genre_of.items():
        by_genre.setdefault(g, []).append(k)
    events: list[SignalEvent] = []
    days = 400

    def at(day: float) -> datetime:
        return NOW - timedelta(days=days - day)

    liked = [("crime", 0.6), ("anim", 0.4)]
    picks = []
    for g, share in liked:
        pool = list(by_genre[g])
        rng.shuffle(pool)
        picks += [(k, g) for k in pool[: min(int(70 * share), int(len(pool) * 0.8))]]
    rng.shuffle(picks)
    for i, (k, g) in enumerate(picks):
        day = (i + 1) / (len(picks) + 1) * days
        # The adapter polls: several `played` events while watching, then `completed`.
        for p in (10, 30, 55, 80):
            events.append(SignalEvent(k, "movie", "played", 0.3, at(day) - timedelta(minutes=100 - p), "jellyfin", progress_pct=p))
        events.append(SignalEvent(k, "movie", "completed", 1.0, at(day), "jellyfin", progress_pct=100))
        if rng.random() < 0.25:
            events.append(SignalEvent(k, "movie", "rated", 2.0 if rng.random() < 0.5 else 1.0, at(day) + timedelta(hours=1), "jellyfin", metadata={"value": 2}))
    # dislikes: horror, started and thumbed down / abandoned early
    for j, k in enumerate(by_genre["horror"][:8]):
        day = 20 + j * 40
        events.append(SignalEvent(k, "movie", "played", 0.3, at(day), "jellyfin", progress_pct=8))
        if j % 2 == 0:
            events.append(SignalEvent(k, "movie", "rated", -1.0, at(day) + timedelta(hours=1), "jellyfin", metadata={"value": -1}))
    # the binge: 40 episodes of one docu series, all `completed` (legacy counts each one)
    series = by_genre["docu"][0]
    for ep in range(40):
        day = 300 + ep * 0.5
        events.append(SignalEvent(series, "series_episode", "played", 0.3, at(day) - timedelta(minutes=20), "jellyfin", progress_pct=40, sub_key=f"e{ep}"))
        events.append(SignalEvent(series, "series_episode", "completed", 1.0, at(day), "jellyfin", progress_pct=100, sub_key=f"e{ep}"))
    events.sort(key=lambda e: e.timestamp)
    return events
