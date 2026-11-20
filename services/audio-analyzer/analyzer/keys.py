"""Musical keys, the Camelot wheel, and how well two keys mix.

Camelot codes: 1-12 around the circle of fifths, A = minor, B = major. Moving one step
(8A -> 9A), switching mode on the same number (8A -> 8B), or staying put are smooth
transitions; DJs call this harmonic mixing.
"""

from __future__ import annotations

PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# pitch class -> camelot number, major (B) and minor (A)
_MAJOR = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}
_MINOR = {9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7}


def camelot(pitch_class: int, mode: str) -> str:
    return f"{(_MAJOR if mode == 'major' else _MINOR)[pitch_class % 12]}{'B' if mode == 'major' else 'A'}"


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
