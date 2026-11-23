"""CLAP: one embedding space for sound and text (LAION, via Hugging Face transformers).

Audio embeddings say what a track *sounds* like — instrumentation, texture, vocals, production,
mood — so "sounds like" and taste in sound work without tags. Text embeddings in the same space
let you search by description ("dreamy synths, slow, female vocals") and name what a cluster of
tracks has in common (taste portraits).

MUSE_AUDIO_MODEL (default laion/clap-htsat-unfused, ~600 MB), MUSE_DEVICE auto|cpu|cuda.
"""

from __future__ import annotations

import logging
import os
import threading

import numpy as np

log = logging.getLogger("muse.audio.embed")

MODEL_NAME = os.environ.get("MUSE_AUDIO_MODEL", "laion/clap-htsat-unfused")
CLAP_SR = 48000
DIM = 512


class Clap:
    def __init__(self, model_name: str = MODEL_NAME, device: str | None = None) -> None:
        import torch
        from transformers import AutoFeatureExtractor, AutoTokenizer, ClapModel

        want = (device or os.environ.get("MUSE_DEVICE", "auto")).lower()
        self.device = "cuda" if want in ("auto", "cuda", "gpu") and torch.cuda.is_available() else "cpu"
        if want in ("cuda", "gpu") and self.device == "cpu":
            log.warning("MUSE_DEVICE=%s but no CUDA device is visible: using CPU", want)
        threads = int(os.environ.get("MUSE_AUDIO_THREADS", "0") or 0)
        if threads and self.device == "cpu":
            torch.set_num_threads(threads)
        self.torch = torch
        self.model = ClapModel.from_pretrained(model_name).to(self.device).eval()
        self.fe = AutoFeatureExtractor.from_pretrained(model_name)
        self.tok = AutoTokenizer.from_pretrained(model_name)
        self.name = model_name
        self.lock = threading.Lock()
        log.info("CLAP %s on %s", model_name, self.device)

    @staticmethod
    def _pooled(out):
        # transformers 5 returns a model output with the normalised projection as pooler_output; 4.x a tensor.
        return out.pooler_output if hasattr(out, "pooler_output") else out

    def audio(self, clips: list[np.ndarray]) -> list[list[float]]:
        """clips: mono float32 at 48 kHz. Returns L2-normalised 512-d vectors."""
        with self.lock, self.torch.inference_mode():
            inputs = self.fe([c.astype(np.float32) for c in clips], sampling_rate=CLAP_SR, return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            emb = self._pooled(self.model.get_audio_features(**inputs))
            emb = self.torch.nn.functional.normalize(emb, dim=-1)
        return emb.cpu().numpy().astype(float).tolist()

    def text(self, texts: list[str]) -> list[list[float]]:
        with self.lock, self.torch.inference_mode():
            inputs = self.tok(texts, padding=True, return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            emb = self._pooled(self.model.get_text_features(**inputs))
            emb = self.torch.nn.functional.normalize(emb, dim=-1)
        return emb.cpu().numpy().astype(float).tolist()


def track_embedding(clap, body: np.ndarray) -> list[float]:
    """Average of three 10 s crops across the body (CLAP-unfused looks at 10 s), re-normalised."""
    win = CLAP_SR * 10
    if len(body) <= win:
        crops = [body]
    else:
        starts = np.linspace(0, len(body) - win, 3).astype(int)
        crops = [body[s:s + win] for s in starts]
    vecs = np.array(clap.audio(crops))
    v = vecs.mean(axis=0)
    return (v / (np.linalg.norm(v) or 1.0)).tolist()
