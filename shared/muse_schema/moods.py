"""Mood presets: what a mood means for recommendations.

Each preset has
  text   — a description embedded in the text space (shapes candidates in every domain),
  sound  — a description embedded in CLAP's audio space (how it should sound; music only),
  audio  — target ranges for audio features (soft: outside the range costs score, never hides),
  explore — how adventurous to be in this mood.
Custom moods ("rainy sunday, cooking for friends") are free text: embedded the same way, with
feature targets guessed from keywords (see guess_targets).
"""

from __future__ import annotations

MOODS: dict[str, dict] = {
    "focus": {"text": "calm instrumental music for deep concentration, steady and unobtrusive",
              "sound": "instrumental, steady, minimal, ambient, no vocals",
              "audio": {"energy": (0.15, 0.55), "bpm": (60, 125), "percussiveness": (0.0, 0.45)}, "explore": 0.05},
    "chill": {"text": "relaxed mellow laid-back music to unwind", "sound": "mellow, relaxed, soft, warm",
              "audio": {"energy": (0.1, 0.5), "bpm": (60, 110)}, "explore": 0.1},
    "energy": {"text": "upbeat high energy music that gets you going", "sound": "high energy, upbeat, driving drums",
               "audio": {"energy": (0.65, 1.0), "bpm": (110, 190)}, "explore": 0.12},
    "workout": {"text": "intense powerful workout music, fast and motivating", "sound": "intense, fast tempo, heavy beat, powerful",
                "audio": {"energy": (0.7, 1.0), "bpm": (120, 190), "danceability": (0.4, 1.0)}, "explore": 0.1},
    "party": {"text": "danceable party music, festive and upbeat", "sound": "danceable, upbeat, festive, club",
              "audio": {"energy": (0.6, 1.0), "danceability": (0.6, 1.0), "bpm": (100, 140)}, "explore": 0.1},
    "sleep": {"text": "very calm soothing music for falling asleep", "sound": "very calm, ambient, soft, slow, quiet",
              "audio": {"energy": (0.0, 0.3), "bpm": (40, 90), "percussiveness": (0.0, 0.3)}, "explore": 0.02},
    "sad": {"text": "melancholic sad emotional music", "sound": "melancholic, sad, slow, emotional",
            "audio": {"energy": (0.1, 0.55), "bpm": (55, 110)}, "mode": "minor", "explore": 0.1},
    "happy": {"text": "happy cheerful feel-good music", "sound": "happy, cheerful, bright, major key",
              "audio": {"energy": (0.45, 0.95)}, "mode": "major", "explore": 0.12},
    "romantic": {"text": "romantic intimate love songs", "sound": "romantic, intimate, soft vocals, warm",
                 "audio": {"energy": (0.15, 0.6)}, "explore": 0.1},
    "cooking": {"text": "feel-good groovy music for cooking at home", "sound": "groovy, warm, feel-good, mid-tempo",
                "audio": {"energy": (0.4, 0.8), "bpm": (85, 125)}, "explore": 0.15},
    "commute": {"text": "music for the commute, steady and engaging", "sound": "steady, engaging, mid-tempo",
                "audio": {"energy": (0.4, 0.85)}, "explore": 0.15},
    "dinner": {"text": "soft background music for dinner, jazz and soul", "sound": "soft, jazzy, smooth, background",
               "audio": {"energy": (0.15, 0.55), "acousticness": (0.4, 1.0)}, "explore": 0.08},
    "rainy": {"text": "cozy music for a rainy day, introspective and warm", "sound": "cozy, introspective, soft, acoustic",
              "audio": {"energy": (0.1, 0.55), "acousticness": (0.3, 1.0)}, "explore": 0.12},
    "discover": {"text": "something new and surprising", "sound": "", "audio": {}, "explore": 0.5},
    # Mostly for films and shows.
    "light": {"text": "light-hearted, funny and easy to watch", "sound": "", "audio": {}, "explore": 0.12},
    "intense": {"text": "gripping, intense and suspenseful", "sound": "tense, dark, intense", "audio": {"energy": (0.5, 1.0)}, "explore": 0.12},
    "cozy": {"text": "comforting, cozy and heartwarming", "sound": "warm, gentle, acoustic", "audio": {"energy": (0.1, 0.55)}, "explore": 0.08},
}

_KEYWORDS = [
    (("sleep", "slaap", "bed", "night-time", "lullaby"), "sleep"),
    (("focus", "work", "werk", "study", "studeren", "concentr"), "focus"),
    (("workout", "gym", "run", "sport", "hardlopen", "train"), "workout"),
    (("party", "feest", "dance", "dansen"), "party"),
    (("sad", "verdrietig", "melanchol", "heartbreak"), "sad"),
    (("happy", "vrolijk", "blij", "sunny", "zon"), "happy"),
    (("chill", "relax", "unwind", "rustig", "ontspan"), "chill"),
    (("cook", "kook", "koken"), "cooking"),
    (("rain", "regen"), "rainy"),
    (("romant", "love", "date"), "romantic"),
    (("dinner", "diner", "eten"), "dinner"),
    (("commute", "car", "auto", "drive", "rijden", "train"), "commute"),
    (("energy", "energie", "pump", "hype"), "energy"),
]


def resolve(mood: str | None) -> tuple[str | None, dict]:
    """(preset name or None, definition). Unknown text becomes a custom mood whose feature targets
    come from the first preset its keywords match."""
    if not mood:
        return None, {}
    m = mood.strip().lower()
    if m in MOODS:
        return m, MOODS[m]
    guess = guess_targets(m)
    return None, {"text": mood.strip(), "sound": mood.strip(), "audio": guess.get("audio", {}),
                  "mode": guess.get("mode"), "explore": guess.get("explore", 0.12)}


def guess_targets(text: str) -> dict:
    t = text.lower()
    for words, preset in _KEYWORDS:
        if any(w in t for w in words):
            return MOODS[preset]
    return {}


def audio_fit(features: dict | None, definition: dict) -> float | None:
    """0..1: how well an item's audio features sit in the mood's ranges (None if unknown)."""
    if not features or not definition.get("audio") and not definition.get("mode"):
        return None
    scores = []
    for key, (lo, hi) in (definition.get("audio") or {}).items():
        v = features.get(key)
        if not isinstance(v, (int, float)):
            continue
        span = max(hi - lo, 1e-6)
        if lo <= v <= hi:
            scores.append(1.0)
        else:
            dist = (lo - v) if v < lo else (v - hi)
            scores.append(max(0.0, 1 - dist / (span * 0.75)))
    if definition.get("mode") and features.get("mode"):
        scores.append(1.0 if features["mode"] == definition["mode"] else 0.4)
    return sum(scores) / len(scores) if scores else None
