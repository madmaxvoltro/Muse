"""Audio features from raw samples (numpy + librosa). Interpretable numbers that describe how
a track sounds, used to filter (moods), to sequence (next track) and to learn a person's
fine-grained preferences.

All in 0..1 unless noted:
  bpm (beats per minute), key / mode / camelot / key_confidence, energy, loudness_db (dBFS),
  dynamics (spread of loudness), brightness (spectral centroid), percussiveness (share of
  percussive energy, HPSS), danceability (beat strength x regularity), acousticness (smooth,
  tonal spectrum with little high-frequency noise), onset_rate (onsets per second).
"""

from __future__ import annotations

import numpy as np

from .keys import PITCHES, camelot

SR = 22050

# Krumhansl-Schmuckler key profiles.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _clip01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def estimate_key(chroma_mean: np.ndarray) -> tuple[int, str, float]:
    """(pitch class, 'major'|'minor', confidence 0..1) by correlating with rotated profiles."""
    scores = []
    for pc in range(12):
        for mode, prof in (("major", _MAJOR), ("minor", _MINOR)):
            r = np.corrcoef(chroma_mean, np.roll(prof, pc))[0, 1]
            scores.append((0.0 if np.isnan(r) else float(r), pc, mode))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    conf = _clip01((best[0] - second[0]) * 5 + max(0.0, best[0]) * 0.5)
    return best[1], best[2], conf


def analyze(y: np.ndarray, sr: int = SR) -> dict:
    import librosa

    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=0)
    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR)
        sr = SR
    if len(y) < sr * 3 or float(np.abs(y).max(initial=0.0)) < 1e-4:
        return {}

    rms = librosa.feature.rms(y=y)[0]
    rms_db = 20 * np.log10(np.maximum(rms, 1e-6))
    loudness_db = float(20 * np.log10(max(float(np.sqrt(np.mean(y ** 2))), 1e-6)))
    energy = _clip01((np.percentile(rms_db, 75) + 40) / 34)  # -40 dBFS -> 0, -6 dBFS -> 1
    dynamics = _clip01(float(np.std(rms_db)) / 12)

    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    if beats is not None and len(beats) > 3:
        intervals = np.diff(librosa.frames_to_time(beats, sr=sr))
        regularity = _clip01(1 - float(np.std(intervals) / max(np.mean(intervals), 1e-6)) * 4)
        beat_strength = _clip01(float(np.mean(onset_env[beats]) / (np.mean(onset_env) + 1e-6) - 1))
    else:
        regularity, beat_strength = 0.0, 0.0
    tempo_fit = _clip01(1 - abs(bpm - 118) / 70)  # danceable tempos cluster around 100-130
    danceability = _clip01(0.45 * regularity + 0.35 * beat_strength + 0.2 * tempo_fit)

    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr)
    onset_rate = float(len(onsets) / (len(y) / sr))

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    brightness = _clip01(float(np.median(centroid)) / 4000)
    flatness = float(np.median(librosa.feature.spectral_flatness(y=y)[0]))

    harmonic, percussive = librosa.effects.hpss(y)
    h_e, p_e = float(np.sum(harmonic ** 2)), float(np.sum(percussive ** 2))
    percussiveness = _clip01(p_e / (h_e + p_e + 1e-9))
    acousticness = _clip01(1 - (0.5 * percussiveness + 0.3 * brightness + 0.2 * _clip01(flatness * 20)) * 1.4)

    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr)
    pc, mode, key_conf = estimate_key(chroma.mean(axis=1))

    return {
        "bpm": round(bpm, 1), "key": f"{PITCHES[pc]} {mode}", "mode": mode, "camelot": camelot(pc, mode),
        "key_confidence": round(key_conf, 3), "energy": round(energy, 3), "loudness_db": round(loudness_db, 1),
        "dynamics": round(dynamics, 3), "brightness": round(brightness, 3), "percussiveness": round(percussiveness, 3),
        "danceability": round(danceability, 3), "acousticness": round(acousticness, 3), "onset_rate": round(onset_rate, 2),
    }


SUMMARY_KEYS = ("bpm", "energy", "loudness_db", "brightness", "percussiveness", "danceability", "acousticness")


def summarize(body: dict, intro: dict | None = None, outro: dict | None = None) -> dict:
    """The track's features (from its body), plus intro/outro tempo and energy for sequencing."""
    out = dict(body)
    for name, seg in (("intro", intro), ("outro", outro)):
        if seg:
            out[name] = {k: seg[k] for k in ("bpm", "energy", "camelot") if k in seg}
    return out
