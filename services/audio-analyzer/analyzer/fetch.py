"""Decode a piece of a remote (or local) audio file to mono float32 with ffmpeg.

ffmpeg seeks with HTTP range requests, so analysing 40 s from the middle of a 40 MB FLAC on
Mono transfers a few MB, not the whole file.
"""

from __future__ import annotations

import asyncio

import numpy as np


async def decode(url: str, start: float = 0.0, duration: float = 30.0, sr: int = 48000, timeout: float = 120) -> np.ndarray:
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        args += ["-ss", f"{start:.2f}"]
    args += ["-t", f"{duration:.2f}", "-i", url, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg timed out")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[:300]}")
    return np.frombuffer(out, dtype=np.float32).copy()


def segments(duration_s: float | None, preview: bool) -> dict[str, tuple[float, float]]:
    """Which parts to decode: a 30 s preview is one body; a full track gets intro/body/outro."""
    if preview or not duration_s or duration_s < 75:
        return {"body": (0.0, min(duration_s or 30.0, 40.0))}
    body_start = max(20.0, duration_s * 0.35)
    return {
        "intro": (0.0, 20.0),
        "body": (min(body_start, duration_s - 45.0), 40.0),
        "outro": (max(0.0, duration_s - 22.0), 20.0),
    }
