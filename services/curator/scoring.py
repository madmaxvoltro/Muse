"""Curator scoring with the same engine the recommendation API uses (muse_taste), so what
the curator downloads is what Muse would recommend, and what it removes is what no profile
would miss.

Multi-profile rule (docs/architecture.md):
  - ADD: averaged score across active profiles.
  - REMOVE: removal-eligible only if *every* active profile scores it low (max < threshold).
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import numpy as np
from muse_schema import canon_point_id
from muse_taste.profile import TasteProfile
from muse_taste.ranking import CandidateInput, Scored, score_candidates
from muse_schema.crypto import P_PROFILE, get_vault
from muse_taste.store import ITEMS_COLLECTION, blind_canon
from qdrant_client import QdrantClient

ADD_SCORE_THRESHOLD = float(os.environ.get("MUSE_CURATOR_ADD_SCORE", "0.55"))
REMOVE_SCORE_THRESHOLD = float(os.environ.get("MUSE_CURATOR_REMOVE_SCORE", "0.10"))

# arr item types -> canonical TMDB kind
ARR_KIND = {"movie": "movie", "series_episode": "tv", "series": "tv"}


def real_canon_for(arr_item_type: str, tmdb_id: str) -> str:
    return f"tmdb:{ARR_KIND.get(arr_item_type, 'movie')}:{tmdb_id}"


def canon_for(arr_item_type: str, tmdb_id: str) -> str:
    """The stored (blind) canonical key, as used in profiles and the vector store."""
    return blind_canon(real_canon_for(arr_item_type, tmdb_id))


class ProfileScorer:
    def __init__(self, qdrant: QdrantClient, profiles: dict[str, TasteProfile]):
        self.qdrant = qdrant
        self.profiles = profiles
        self._anchors: dict[str, dict[str, np.ndarray]] = {}

    @classmethod
    async def load(cls, pool: asyncpg.Pool, qdrant: QdrantClient, user_ids: list[str], domain: str = "video") -> "ProfileScorer":
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, profile FROM taste_profile WHERE domain = $1 AND user_id = ANY($2::uuid[])",
                domain,
                [uuid.UUID(u) for u in user_ids],
            )
        profiles = {}
        for r in rows:
            profiles[str(r["user_id"])] = TasteProfile.from_json(get_vault().open_json(r["profile"], P_PROFILE))
        return cls(qdrant, profiles)

    def vectors(self, keys: list[str]) -> dict[str, np.ndarray]:
        ids = {canon_point_id(k): k for k in keys}
        out = {}
        idl = list(ids)
        for i in range(0, len(idl), 256):
            for p in self.qdrant.retrieve(ITEMS_COLLECTION, ids=idl[i : i + 256], with_vectors=True):
                out[ids[str(p.id)]] = np.asarray(p.vector, dtype=np.float32)
        return out

    def _anchor_vecs(self, uid: str) -> dict[str, np.ndarray]:
        if uid not in self._anchors:
            self._anchors[uid] = self.vectors([a.key for a in self.profiles[uid].anchors[:300]])
        return self._anchors[uid]

    def seen_by_anyone(self) -> set[str]:
        return {k for p in self.profiles.values() for k in p.seen}

    def score(self, cands: list[CandidateInput]) -> dict[str, dict[str, Scored]]:
        """-> {canon_key: {user_id: Scored}}"""
        out: dict[str, dict[str, Scored]] = {c.key: {} for c in cands}
        for uid, prof in self.profiles.items():
            rel = {r["key"]: r for r in prof.related}
            per_user = [
                CandidateInput(
                    key=c.key, vector=c.vector, tags=c.tags, vote_average=c.vote_average, vote_count=c.vote_count,
                    related_support=float(rel.get(c.key, {}).get("support", 0.0)), related_via=list(rel.get(c.key, {}).get("via", [])),
                    payload=c.payload,
                )
                for c in cands
            ]
            for s in score_candidates(prof, per_user, anchor_vectors=self._anchor_vecs(uid)):
                out[s.key][uid] = s
        return out


def add_score(per_user: dict[str, Scored]) -> float:
    return float(np.mean([s.score for s in per_user.values()])) if per_user else 0.0


def remove_score(per_user: dict[str, Scored]) -> float | None:
    return max((s.score for s in per_user.values()), default=None)
