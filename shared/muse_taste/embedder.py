"""Pluggable text embedders.

MUSE_EMBED_PROVIDER=local (default)
    sentence-transformers in-process. Device: MUSE_EMBED_DEVICE=auto|cpu|cuda|mps (auto picks
    a GPU when present). Default model Qwen/Qwen3-Embedding-0.6B: multilingual (Dutch
    descriptions work), 32k context, Apache-2.0, among the strongest sub-1B models on MTEB,
    ~1.2 GB in fp16 on GPU / ~2.4 GB fp32 on CPU. Lighter options: google/embeddinggemma-300m
    (gated on Hugging Face: needs HF_TOKEN), BAAI/bge-small-en-v1.5, all-MiniLM-L6-v2.

MUSE_EMBED_PROVIDER=openai
    Any OpenAI-compatible /v1/embeddings endpoint: Ollama, vLLM, LM Studio, llama.cpp server,
    Hugging Face TEI, LocalAI, OpenAI itself. MUSE_EMBED_BASE_URL, MUSE_EMBED_API_KEY,
    MUSE_EMBEDDING_MODEL. An endpoint outside your network needs MUSE_EMBED_ALLOW_EXTERNAL=true:
    item descriptions are not personal, but which titles you own is.

MUSE_EMBED_DIM truncates (Matryoshka models such as Qwen3/EmbeddingGemma keep most quality at
256-512 dims and halve memory). Changing model or dim triggers a full re-embed in the worker.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import numpy as np

from .privacy import require_private_or_allowed

logger = logging.getLogger("muse.embedder")

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"

# Query-side instructions for instruction-tuned embedders (documents are embedded plain).
QUERY_INSTRUCTION = "Instruct: Given a description of a mood, topic or taste, retrieve films, shows, music or videos that match it\nQuery: "


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def _unit(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.where(n == 0, 1.0, n)


class LocalEmbedder:
    def __init__(self, model_name: str, device: str = "auto", batch_size: int = 16, dim: int | None = None, max_seq_length: int = 512):
        from sentence_transformers import SentenceTransformer

        if device == "auto":
            device = _auto_device()
        kwargs: dict = {}
        if device.startswith("cuda"):
            try:
                import torch

                kwargs["model_kwargs"] = {"torch_dtype": torch.float16}
            except Exception:  # pragma: no cover
                pass
        logger.info("loading local embedder %s on %s", model_name, device)
        self.model = SentenceTransformer(model_name, device=device, **kwargs)
        try:
            self.model.max_seq_length = min(self.model.max_seq_length or max_seq_length, max_seq_length)
        except Exception:  # pragma: no cover
            pass
        self.name = model_name
        self.batch_size = batch_size
        native = self.model.get_sentence_embedding_dimension() or len(self.model.encode(["x"])[0])
        self.dim = min(dim, native) if dim else native
        self._has_query_prompt = "query" in (getattr(self.model, "prompts", None) or {})

    def embed(self, texts: list[str]) -> np.ndarray:
        v = self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True, show_progress_bar=False)
        return _unit(np.asarray(v, dtype=np.float32)[:, : self.dim])

    def embed_query(self, text: str) -> np.ndarray:
        if self._has_query_prompt:
            v = self.model.encode([text], prompt_name="query", normalize_embeddings=True)
        elif "qwen" in self.name.lower() or "e5" in self.name.lower():
            v = self.model.encode([QUERY_INSTRUCTION + text], normalize_embeddings=True)
        else:
            v = self.model.encode([text], normalize_embeddings=True)
        return _unit(np.asarray(v, dtype=np.float32)[:, : self.dim])[0]


class OpenAICompatibleEmbedder:
    def __init__(self, base_url: str, model_name: str, api_key: str | None = None, dim: int | None = None, batch_size: int = 64, timeout: float = 120.0):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.name = model_name
        self.batch_size = batch_size
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        self._dim_override = dim
        probe = self._call(["dimension probe"])
        self.dim = min(dim, probe.shape[1]) if dim else probe.shape[1]

    def _call(self, texts: list[str]) -> np.ndarray:
        resp = self._client.post(f"{self.base_url}/embeddings", json={"model": self.name, "input": texts})
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
        return np.asarray([d["embedding"] for d in data], dtype=np.float32)

    def embed(self, texts: list[str]) -> np.ndarray:
        out = []
        for i in range(0, len(texts), self.batch_size):
            out.append(self._call(texts[i : i + self.batch_size]))
        v = np.vstack(out) if out else np.zeros((0, self.dim), np.float32)
        return _unit(v[:, : self.dim])

    def embed_query(self, text: str) -> np.ndarray:
        prefix = QUERY_INSTRUCTION if "qwen" in self.name.lower() else ""
        return self.embed([prefix + text])[0]


def _auto_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # pragma: no cover
        pass
    return "cpu"


def from_env(env: dict[str, str] | None = None) -> Embedder:
    env = dict(os.environ) if env is None else env
    provider = env.get("MUSE_EMBED_PROVIDER", "local").lower()
    model = env.get("MUSE_EMBEDDING_MODEL", DEFAULT_MODEL)
    dim = int(env["MUSE_EMBED_DIM"]) if env.get("MUSE_EMBED_DIM") else None
    if provider in ("openai", "ollama", "vllm", "tei", "remote"):
        base = env.get("MUSE_EMBED_BASE_URL") or "http://ollama:11434/v1"
        require_private_or_allowed(base, env.get("MUSE_EMBED_ALLOW_EXTERNAL", "false"), what="embedding endpoint")
        return OpenAICompatibleEmbedder(base, model, env.get("MUSE_EMBED_API_KEY"), dim, int(env.get("MUSE_EMBED_BATCH_SIZE", "64")))
    return LocalEmbedder(
        model,
        device=env.get("MUSE_EMBED_DEVICE", "auto"),
        batch_size=int(env.get("MUSE_EMBED_BATCH_SIZE", "16")),
        dim=dim,
        max_seq_length=int(env.get("MUSE_EMBED_MAX_TOKENS", "512")),
    )
