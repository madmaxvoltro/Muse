"""Audio analyzer: ffmpeg decoding, features, the worker cycle (fake CLAP), key/tempo maths."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid

import asyncpg
import numpy as np
import pytest
from qdrant_client import QdrantClient

from conftest import ROOT

sys.path.insert(0, str(ROOT / "services" / "audio-analyzer"))
pytest.importorskip("librosa")
from analyzer import features as F, fetch, keys, service as S  # noqa: E402

SR = 48000


def tone(seconds: float, bpm: float = 120, chord=(261.63, 329.63, 392.0), sr: int = SR) -> np.ndarray:
    t = np.arange(int(sr * seconds)) / sr
    y = 0.12 * sum(np.sin(2 * np.pi * f * t) for f in chord)
    click = np.hanning(400) * np.random.RandomState(0).randn(400)
    for b in np.arange(0, seconds, 60 / bpm):
        i = int(b * sr)
        y[i:i + 400] += 0.8 * click[: len(y[i:i + 400])]
    return y.astype(np.float32)


def test_keys_and_tempo():
    assert keys.camelot(0, "major") == "8B" and keys.camelot(9, "minor") == "8A"
    assert keys.key_compatibility("8A", "8A") == 1.0 > keys.key_compatibility("8A", "9A") > keys.key_compatibility("8A", "3B")
    assert keys.tempo_compatibility(87, 174) == 1.0 and keys.tempo_compatibility(120, 150) == 0.0


def test_features_on_synthetic_music():
    f = F.analyze(tone(20, 120), SR)
    assert 110 <= f["bpm"] <= 130 and f["camelot"] == "8B" and f["key"] == "C major"
    minor = F.analyze(tone(20, 90, chord=(220.0, 261.63, 329.63)), SR)
    assert minor["mode"] == "minor" and minor["camelot"] == "8A"
    assert F.analyze(np.zeros(SR * 10, dtype=np.float32), SR) == {}  # silence


async def test_ffmpeg_decode(tmp_path):
    wav = tmp_path / "t.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=12", str(wav)], check=True)
    y = await fetch.decode(str(wav), start=2, duration=5, sr=SR)
    assert abs(len(y) - 5 * SR) < SR * 0.1
    assert fetch.segments(240, preview=False).keys() == {"intro", "body", "outro"}
    assert fetch.segments(30, preview=True).keys() == {"body"}


class FakeClap:
    name = "fake-clap"

    def audio(self, clips):
        return [[float(np.mean(c))] + [0.01] * 511 for c in clips]


async def test_worker_cycle(db, monkeypatch):
    conn = await asyncpg.connect(db)
    uid = uuid.uuid4()
    await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, metadata) VALUES "
                       "($1, 'mono', 't1', 'track', 'cataloged', $2::jsonb)", uid,
                       json.dumps({"title": "A - Song", "artist": "A", "duration_s": 200}))
    await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, metadata) VALUES "
                       "($1, 'catalog', 'dz:5', 'track', 'cataloged', $2::jsonb)", uid, json.dumps({"title": "B - x", "deezer_id": 5}))
    await conn.execute("INSERT INTO item_audio (source, source_item_id) VALUES ('catalog', 'dz:5')")
    monkeypatch.setattr(S, "MONO_URL", "http://mono")
    monkeypatch.setattr(S, "MONO_STREAM_TOKEN", "stream-secret")
    seen = []

    async def decoder(url, start, length, sr):
        seen.append((url, start, length))
        return tone(min(length, 20), 128)

    class Http:  # Deezer: fresh preview URL
        async def get(self, url):
            class R:
                status_code = 200

                def json(self):
                    return {"preview": "https://cdn/p.mp3"}
            return R()

    qd = QdrantClient(":memory:")
    S.ensure_collection(qd)
    pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
    stats = await S.run_cycle(pool, qd, Http(), FakeClap(), decoder)
    await pool.close()
    assert stats["done"] == 2 and stats["queued"] == 1
    assert ("http://mono/api/tracks/t1/stream?t=stream-secret", 0.0, 20.0) in seen  # intro of a full track
    assert any(u == "https://cdn/p.mp3" for u, _, _ in seen)
    row = await conn.fetchrow("SELECT * FROM item_audio WHERE source = 'mono'")
    feats = json.loads(row["features"])
    assert row["status"] == "done" and row["has_embedding"] and "intro" in feats and "outro" in feats
    assert 118 <= feats["bpm"] <= 138
    from muse_schema import content_point_id

    p = qd.retrieve("audio_items", [content_point_id("mono", "t1")], with_payload=True)[0]
    assert p.payload["camelot"] == feats["camelot"] and p.payload["title"] == "A - Song"
    await conn.close()
