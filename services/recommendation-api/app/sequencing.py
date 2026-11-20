"""What should play next, and whole radio sessions.

A good next song has to fit the *person* (their taste, facets, preferences, mood) and fit the
*previous song*: it should sound related, mix harmonically (Camelot wheel: same key, one step,
or relative major/minor), sit at a compatible tempo (including half/double time), follow the
session's energy curve, and preferably be something that has followed it well in this household
before (transitions that were completed rather than skipped). Artists aren't repeated back to back.

    transition = 0.32 taste + 0.18 sound + 0.06 text + 0.10 key + 0.10 tempo + 0.10 energy
               + 0.12 played-after + 0.02 behaviour - repeat penalties
    (for build / wind_down / peak the energy curve weighs 0.38 and sound/tempo less)

Radio = beam search over transitions: several partial sessions are kept and extended, so one
greedy choice can't paint the session into a corner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from muse_schema.audio import key_compatibility, tempo_compatibility

from .engine import Item

W = {"taste": 0.32, "sound": 0.18, "text": 0.06, "key": 0.10, "tempo": 0.10, "energy": 0.10, "after": 0.12, "behavior": 0.02}
# When the session should change (build up, wind down, peak), following the energy curve matters
# more than sounding like the previous song.
W_MOVING = {"taste": 0.25, "sound": 0.10, "text": 0.04, "key": 0.08, "tempo": 0.05, "energy": 0.38, "after": 0.08, "behavior": 0.02}
ARCS = ("steady", "build", "wind_down", "peak")


@dataclass
class Track:
    """Whatever we know about one song, current or candidate."""
    key: str
    title: str = ""
    artist: str = ""
    audio: dict = field(default_factory=dict)
    tv: np.ndarray | None = None
    av: np.ndarray | None = None
    bv: np.ndarray | None = None
    taste: float = 0.5  # normalised score from the engine, 0..1
    item: Item | None = None


def _unit(v) -> np.ndarray | None:
    if v is None:
        return None
    a = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(a)
    return a / n if n else a


def from_item(it: Item, taste: float) -> Track:
    return Track(it.key, it.title, it.artist, it.audio or {}, _unit(it.tv), _unit(it.av), _unit(it.bv), taste, it)


def _sim(a, b) -> float | None:
    if a is None or b is None:
        return None
    return float(a @ b)


def target_energy(arc: str, start: float, position: int, length: int) -> float:
    x = position / max(length - 1, 1)
    if arc == "build":
        return min(1.0, start + 0.35 * x)
    if arc == "wind_down":
        return max(0.05, start - (start - 0.15) * x)
    if arc == "peak":
        return min(1.0, start + 0.35 * math.sin(math.pi * x))
    return start


def transition(cur: Track, nxt: Track, energy_target: float | None, after: dict[tuple[str, str], float],
               recent_artists: list[str], moving: bool = False) -> tuple[float, dict[str, float]]:
    out_f = cur.audio.get("outro") or cur.audio
    in_f = nxt.audio.get("intro") or nxt.audio
    parts = {
        "taste": nxt.taste,
        "sound": max(0.0, _sim(cur.av, nxt.av)) if cur.av is not None and nxt.av is not None else 0.4,
        "text": max(0.0, _sim(cur.tv, nxt.tv)) if cur.tv is not None and nxt.tv is not None else 0.3,
        "key": key_compatibility(out_f.get("camelot") or cur.audio.get("camelot"), in_f.get("camelot") or nxt.audio.get("camelot")),
        "tempo": tempo_compatibility(out_f.get("bpm") or cur.audio.get("bpm"), in_f.get("bpm") or nxt.audio.get("bpm")),
        "energy": (1 - min(1.0, abs(energy_target - nxt.audio["energy"]) * 2)) if energy_target is not None and "energy" in nxt.audio else 0.5,
        "after": after.get((cur.key, nxt.key), 0.0),
        "behavior": max(0.0, _sim(cur.bv, nxt.bv)) if cur.bv is not None and nxt.bv is not None else 0.0,
    }
    weights = W_MOVING if moving else W
    s = sum(weights[k] * v for k, v in parts.items())
    if nxt.artist and cur.artist and nxt.artist.casefold() == cur.artist.casefold():
        s -= 0.25
        parts["same_artist"] = -0.25
    elif nxt.artist and nxt.artist.casefold() in recent_artists:
        s -= 0.08
        parts["recent_artist"] = -0.08
    return s, parts


def explain(cur: Track, nxt: Track, parts: dict[str, float]) -> list[str]:
    why = []
    if parts.get("after", 0) >= 0.4:
        why.append("Often played after this here")
    if parts.get("sound", 0) >= 0.6:
        why.append("Sounds close to the previous song")
    if parts.get("key", 0) >= 0.9 and nxt.audio.get("camelot"):
        why.append(f"Mixes harmonically ({(cur.audio.get('outro') or cur.audio).get('camelot')} → {nxt.audio.get('camelot')})")
    if parts.get("tempo", 0) >= 0.8 and nxt.audio.get("bpm"):
        why.append(f"Matching tempo (~{round(nxt.audio['bpm'])} BPM)")
    if parts.get("taste", 0) >= 0.7:
        why.append("Right up your alley")
    return why[:3]


def next_tracks(cur: Track, pool: list[Track], n: int, after: dict[tuple[str, str], float], *, arc: str = "steady",
                history_artists: list[str] | None = None) -> list[tuple[Track, float, dict]]:
    start = cur.audio.get("energy", 0.5)
    target = target_energy(arc, start, 1, 4)
    recent = [a.casefold() for a in (history_artists or [])[-4:]]
    scored = [(t, *transition(cur, t, target, after, recent, arc != "steady")) for t in pool if t.key != cur.key]
    scored.sort(key=lambda x: -x[1])
    return scored[:n]


def radio(seed: Track, pool: list[Track], length: int, after: dict[tuple[str, str], float], *, arc: str = "steady",
          beam: int = 5, branch: int = 8) -> list[tuple[Track, float, dict]]:
    """Beam search for a session of `length` tracks starting after `seed`."""
    start = seed.audio.get("energy", 0.5)
    beams: list[tuple[float, list[tuple[Track, float, dict]]]] = [(0.0, [])]
    for pos in range(length):
        target = target_energy(arc, start, pos + 1, length + 1)
        grown = []
        for total, seq in beams:
            cur = seq[-1][0] if seq else seed
            used = {seed.key, *(t.key for t, _, _ in seq)}
            recent = [t.artist.casefold() for t, _, _ in seq[-4:] if t.artist]
            options = [(t, *transition(cur, t, target, after, recent, arc != "steady")) for t in pool if t.key not in used]
            options.sort(key=lambda x: -x[1])
            for t, s, parts in options[:branch]:
                grown.append((total + s, seq + [(t, s, parts)]))
        if not grown:
            break
        grown.sort(key=lambda x: -x[0])
        beams = grown[:beam]
    return beams[0][1] if beams else []
