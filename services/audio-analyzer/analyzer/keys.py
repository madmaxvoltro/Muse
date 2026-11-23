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


from muse_schema.audio import key_compatibility, parse_camelot, tempo_compatibility  # noqa: E402,F401  (shared with ranking)
