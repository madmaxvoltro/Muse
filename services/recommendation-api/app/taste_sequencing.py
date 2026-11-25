"""What plays next, and radio sessions, on top of the taste engine.

A good next song fits the *person* (the engine's score for it: interests, anchors, evolution,
mood) and the *previous song*: it should be close in content (the item vectors), not by the same
artist twice in a row, and, for radio, keep a sense of movement instead of circling one spot.

    transition = 0.55 taste + 0.35 closeness to the previous song + 0.10 closeness to the seed
                 - artist repetition

Loved songs are radio material too (a radio isn't only discovery): `discovery` 0..1 decides how
much unheard songs are favoured over favourites. Beam search keeps several partial sessions so
one greedy pick can't paint the session into a corner. Everything stays on the server's blind
keys; titles are opened from the sealed payload only to spot the artist.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

W_TASTE, W_PREV, W_SEED = 0.55, 0.35, 0.10


@dataclass
class Track:
    key: str  # blind canonical key
    vector: np.ndarray
    payload: dict
    taste: float  # 0..1
    artist: str = ""

    @property
    def title(self) -> str:
        return self.payload.get("title") or ""


def artist_of(payload: dict) -> str:
    """Music titles are stored as 'Artist - Title' (Mono, Navidrome)."""
    t = payload.get("title") or ""
    return t.split(" - ", 1)[0].strip().casefold() if " - " in t else ""


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


def transition(prev: Track, nxt: Track, seed: Track | None, recent_artists: list[str]) -> tuple[float, dict[str, float]]:
    close = max(0.0, float(_unit(prev.vector) @ _unit(nxt.vector)))
    to_seed = max(0.0, float(_unit(seed.vector) @ _unit(nxt.vector))) if seed is not None else close
    parts = {"taste": nxt.taste, "flow": close, "seed": to_seed}
    s = W_TASTE * nxt.taste + W_PREV * close + W_SEED * to_seed
    if nxt.artist and nxt.artist == prev.artist:
        s -= 0.3
        parts["same_artist"] = -0.3
    elif nxt.artist and nxt.artist in recent_artists:
        s -= 0.1
        parts["recent_artist"] = -0.1
    return s, parts


def next_tracks(cur: Track, pool: list[Track], n: int, history_artists: list[str] | None = None) -> list[tuple[Track, float, dict]]:
    recent = (history_artists or [])[-4:]
    scored = [(t, *transition(cur, t, cur, recent)) for t in pool if t.key != cur.key]
    scored.sort(key=lambda x: -x[1])
    return scored[:n]


def radio(seed: Track, pool: list[Track], length: int, beam: int = 5, branch: int = 8) -> list[tuple[Track, float, dict]]:
    beams: list[tuple[float, list[tuple[Track, float, dict]]]] = [(0.0, [])]
    for _ in range(length):
        grown = []
        for total, seq in beams:
            cur = seq[-1][0] if seq else seed
            used = {seed.key, *(t.key for t, _, _ in seq)}
            recent = [t.artist for t, _, _ in seq[-4:] if t.artist]
            options = [(t, *transition(cur, t, seed, recent)) for t in pool if t.key not in used]
            options.sort(key=lambda x: -x[1])
            for t, s, parts in options[:branch]:
                grown.append((total + s, seq + [(t, s, parts)]))
        if not grown:
            break
        grown.sort(key=lambda x: -x[0])
        beams = grown[:beam]
    return beams[0][1] if beams else []


def why(prev: Track, parts: dict[str, float]) -> list[str]:
    out = []
    if parts.get("flow", 0) >= 0.6:
        out.append(f"Flows on from {prev.title}" if prev.title else "Flows on from the previous song")
    if parts.get("taste", 0) >= 0.7:
        out.append("Right up your alley")
    return out
