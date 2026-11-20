"""Audio feature vector shared by the preference-model trainer (embedding worker) and its
use at ranking time (recommendation API), so both always agree on the layout."""

from __future__ import annotations

PREF_FEATURES = ("energy", "danceability", "acousticness", "brightness", "percussiveness", "dynamics", "tempo", "major", "loudness")


def feature_vector(f: dict) -> list[float] | None:
    if not f or "energy" not in f:
        return None
    return [
        float(f.get("energy", 0.5)), float(f.get("danceability", 0.5)), float(f.get("acousticness", 0.5)),
        float(f.get("brightness", 0.5)), float(f.get("percussiveness", 0.5)), float(f.get("dynamics", 0.5)),
        min(max((f.get("bpm") or 110) - 60, 0) / 120, 1.5),
        1.0 if f.get("mode") == "major" else 0.0,
        min(max((f.get("loudness_db", -14) + 30) / 25, 0), 1.2),
    ]


# --- harmonic and tempo mixing (Camelot wheel) -----------------------------------------------


def parse_camelot(code: str | None) -> tuple[int, str] | None:
    if not code or len(code) < 2 or code[-1] not in "AB":
        return None
    try:
        n = int(code[:-1])
    except ValueError:
        return None
    return (n, code[-1]) if 1 <= n <= 12 else None


def key_compatibility(a: str | None, b: str | None) -> float:
    """1.0 same key; ~0.9 adjacent / relative; falls off further round the wheel. 0.5 if unknown."""
    pa, pb = parse_camelot(a), parse_camelot(b)
    if pa is None or pb is None:
        return 0.5
    steps = min((pa[0] - pb[0]) % 12, (pb[0] - pa[0]) % 12)
    same_mode = pa[1] == pb[1]
    if steps == 0:
        return 1.0 if same_mode else 0.9
    if steps == 1:
        return 0.9 if same_mode else 0.7
    if steps == 2:
        return 0.6 if same_mode else 0.45
    return max(0.0, 0.5 - 0.08 * steps)


def tempo_compatibility(a: float | None, b: float | None) -> float:
    """Closeness of two tempos, counting half/double time as close (85 -> 170 BPM is a mix)."""
    if not a or not b:
        return 0.5
    best = min(abs(a - b * r) / a for r in (0.5, 1.0, 2.0))
    return max(0.0, 1.0 - best / 0.12)  # within ~12% is mixable, linearly worse until there
