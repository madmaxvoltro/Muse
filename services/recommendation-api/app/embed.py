"""Client for the embedding worker's /embed endpoint (same model as the content vectors).
Cached per text: mood prompts and recent searches are embedded once per process."""

import logging
import os
from collections import OrderedDict

import httpx

logger = logging.getLogger("muse.embed")

EMBED_URL = os.environ.get("MUSE_EMBED_URL", "http://embedding-worker:8002").rstrip("/")
_cache: "OrderedDict[str, list[float]]" = OrderedDict()
_CACHE_MAX = 512
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=1.0))
    return _client


async def embed(texts: list[str]) -> list[list[float] | None]:
    """Vectors for texts, None for any that couldn't be embedded (worker down)."""
    out: list[list[float] | None] = [None] * len(texts)
    missing = [i for i, t in enumerate(texts) if t not in _cache]
    for i, t in enumerate(texts):
        if t in _cache:
            _cache.move_to_end(t)
            out[i] = _cache[t]
    if missing and EMBED_URL:
        try:
            r = await _http().post(f"{EMBED_URL}/embed", json={"texts": [texts[i] for i in missing]})
            r.raise_for_status()
            for i, v in zip(missing, r.json()["vectors"]):
                out[i] = v
                _cache[texts[i]] = v
                while len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("embedding unavailable: %s", exc)
    return out


async def embed_one(text: str) -> list[float] | None:
    return (await embed([text]))[0]


async def healthy() -> bool:
    try:
        r = await _http().get(f"{EMBED_URL}/healthz")
        return r.status_code == 200
    except httpx.HTTPError:
        return False
