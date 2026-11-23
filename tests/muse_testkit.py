"""Shared helpers for the cross-service tests (imported by test modules and conftest.py).
A module of its own, because tests/integration has a conftest.py too and a bare
`from conftest import` would pick whichever was imported first."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_package(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path / "__init__.py", submodule_search_locations=[str(path)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeModel:
    """Bag-of-words hashed into 384 dims, normalised: texts sharing words are similar."""

    dim = 384

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 9) % 2 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def encode(self, texts, normalize_embeddings=True):
        import numpy as np  # like sentence-transformers: numpy out

        if isinstance(texts, str):
            return np.array(self._vec(texts))
        return np.array([self._vec(t) for t in texts])
