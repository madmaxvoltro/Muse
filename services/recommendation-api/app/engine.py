"""Request-time recommendation pipeline (docs/recommender.md):

  profile (stored by the worker)
    + events newer than the profile (a thumbs-down counts on the very next request)
  -> generators: interests / short-term / item-to-item / related / household / backlog / mood / quality
  -> RRF fusion -> exclusion of seen titles
  -> features + score + uncertainty for every candidate (muse_taste.ranking)
  -> calibrated, de-duplicated list with Thompson-sampled explore slots (muse_taste.rerank)
  -> explanations, impression log
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import asyncpg
import numpy as np
from muse_schema import canon_point_id
from muse_taste.canon import domain_of
from muse_taste.evolution import Adjustment, adjustment, apply_importance, stretch_score
from muse_taste.profile import Anchor, Prototype, TasteProfile, unit
from muse_taste.ranking import DEFAULT_WEIGHTS, CandidateInput, Scored, explain, score_candidates
from muse_taste.rerank import daily_seed, select
from muse_taste.retrieval import interest_quotas, rrf
from muse_taste.signals import aggregate, from_rows
from muse_schema.crypto import P_PROFILE, get_vault
from muse_taste.store import ITEMS_COLLECTION, open_canon_data, open_payload
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

logger = logging.getLogger("muse.reco.engine")

# What a request for an item_type actually returns: episodes are never recommended one by one.
TARGET_TYPES = {
    "movie": ["movie"],
    "series": ["series"],
    "series_episode": ["series"],
    "youtube_video": ["youtube_video"],
    "track": ["track"],
    "audiobook": ["audiobook"],
    "ebook": ["ebook"],
}

RECENT_EVENTS_SQL = """
SELECT e.source, e.source_item_id, e.item_type, e.action, e.action_weight, e."timestamp", e.progress_pct, e.metadata,
       i.canon_key, i.parent_key, p.canon_key AS parent_canon
FROM event e
LEFT JOIN item i ON i.item_key = e.source || ':' || e.source_item_id
LEFT JOIN item p ON p.item_key = i.parent_key
WHERE e.user_id = $1 AND e.ingested_at > $2 AND e.source_item_id IS NOT NULL AND e.action <> 'cataloged'
"""


@dataclass
class Request:
    user_id: str
    item_type: str
    limit: int = 20
    explore_ratio: float | None = None
    mood: str | None = None
    surface: str = "home"
    include_unavailable: bool = False
    log: bool = True
    at: datetime | None = None  # "now" for rhythms (testing, or a client in another timezone)


@dataclass
class Result:
    request_id: str
    items: list[Scored]
    profile: TasteProfile | None
    titles: dict[str, str]
    cold_start: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    pool: list[Scored] = field(default_factory=list)  # the whole scored pool, best first


class Engine:
    def __init__(self, pool: asyncpg.Pool, qdrant: QdrantClient, embedder_factory=None):
        self.pool = pool
        self.qdrant = qdrant
        self._embedder_factory = embedder_factory
        self._embedder = None
        self._weights: tuple[float, dict[str, float]] = (0.0, dict(DEFAULT_WEIGHTS))
        self._anchor_cache: dict[tuple[str, str, str], dict[str, np.ndarray]] = {}

    # ---- loading --------------------------------------------------------------------------
    async def weights(self) -> dict[str, float]:
        if time.monotonic() - self._weights[0] > 600:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT weights FROM ranker_model WHERE adopted ORDER BY fitted_at DESC LIMIT 1")
            self._weights = (time.monotonic(), {**DEFAULT_WEIGHTS, **(row["weights"] if row else {})})
        return self._weights[1]

    async def profile(self, user_id: str, domain: str) -> TasteProfile | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT profile, computed_at FROM taste_profile WHERE user_id = $1 AND domain = $2", uuid.UUID(user_id), domain)
        if not row:
            return None
        p = TasteProfile.from_json(get_vault().open_json(row["profile"], P_PROFILE))
        p.computed_at = row["computed_at"].isoformat()
        return p

    async def overlay(self, user_id: str) -> dict:
        async with self.pool.acquire() as conn:
            # SELECT * so a database without migration 008 (evolution_strength) still works
            row = await conn.fetchrow("SELECT * FROM user_overlay WHERE user_id = $1", uuid.UUID(user_id))
        return dict(row) if row else {}

    def catalog_ready(self) -> bool:
        """False on a fresh install until the worker has created the vector collection."""
        if not getattr(self, "_ready", False):
            try:
                self._ready = bool(self.qdrant.collection_exists(ITEMS_COLLECTION))
            except Exception:
                self._ready = False
        return self._ready

    async def titles_for(self, keys: list[str]) -> dict[str, str]:
        """Titles for blind canonical keys (canon_item data is sealed)."""
        if not keys:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT canon_key, data FROM canon_item WHERE canon_key = ANY($1::text[])", list(keys))
        return {r["canon_key"]: open_canon_data(r["data"]).get("title") or "" for r in rows}

    async def canon_items(self, keys: list[str]) -> list[dict]:
        if not keys:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT canon_key, item_type, available, data FROM canon_item WHERE canon_key = ANY($1::text[])", list(keys))
        return [{**open_canon_data(r["data"]), "canon_key": r["canon_key"], "item_type": r["item_type"], "available": r["available"]} for r in rows]

    def embedder(self):
        if self._embedder is None:
            if self._embedder_factory is None:
                raise RuntimeError("no embedder configured")
            try:
                self._embedder = self._embedder_factory()
            except Exception as exc:
                raise RuntimeError(
                    "mood queries need an embedder in the recommendation API: set MUSE_EMBED_PROVIDER=openai "
                    f"(an embedding server) or install sentence-transformers in its image ({exc})"
                ) from exc
        return self._embedder

    # ---- responsiveness ---------------------------------------------------------------------
    async def apply_recent(self, prof: TasteProfile, user_id: str, domain: str, disabled: list[str]) -> None:
        """Folds events that arrived after the profile was computed into it, so feedback is
        visible immediately instead of after the next worker cycle (Wang et al. 2023 measure
        exactly this 'responsiveness')."""
        since = datetime.fromisoformat(prof.computed_at)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(RECENT_EVENTS_SQL, uuid.UUID(user_id), since)
        if not rows:
            return
        sig = aggregate([e for e in from_rows(rows) if domain_of(e.item_type) == domain], disabled_sources=disabled)
        seen = set(prof.seen)
        new_neg, new_pos = [], []
        for k, s in sig.items():
            if s.seen or s.explicit:
                seen.add(k)
            if s.explicit and s.preference < 0:
                new_neg.append((k, s))
            elif s.preference > 0.5:
                new_pos.append((k, s))
        prof.seen = sorted(seen)
        if new_neg or new_pos:
            vecs = self.vectors([k for k, _ in new_neg + new_pos])
            for k, s in new_neg:
                if k in vecs:
                    prof.negatives.insert(0, Prototype(key=k, vector=vecs[k].tolist(), weight=min(-s.preference, 2.0)))
            for k, s in new_pos:
                prof.anchors.insert(0, Anchor(key=k, preference=s.preference, confidence=s.confidence, weight=s.preference))

    # ---- vector store helpers ----------------------------------------------------------------
    def vectors(self, keys: list[str]) -> dict[str, np.ndarray]:
        ids = {canon_point_id(k): k for k in keys}
        out: dict[str, np.ndarray] = {}
        idl = list(ids)
        for i in range(0, len(idl), 256):
            for p in self.qdrant.retrieve(ITEMS_COLLECTION, ids=idl[i : i + 256], with_vectors=True, with_payload=False):
                out[ids[str(p.id)]] = np.asarray(p.vector, dtype=np.float32)
        return out

    def anchor_vectors(self, prof: TasteProfile) -> dict[str, np.ndarray]:
        key = (prof.user_id, prof.domain, prof.computed_at)
        if key not in self._anchor_cache:
            if len(self._anchor_cache) > 64:
                self._anchor_cache.clear()
            self._anchor_cache[key] = self.vectors([a.key for a in prof.anchors[:300]])
        cached = self._anchor_cache[key]
        missing = [a.key for a in prof.anchors[:300] if a.key not in cached]
        return {**cached, **(self.vectors(missing) if missing else {})}

    def _filter(self, types: list[str], include_unavailable: bool) -> qm.Filter:
        must: list = [qm.FieldCondition(key="item_type", match=qm.MatchAny(any=types))]
        if not include_unavailable:
            must.append(qm.FieldCondition(key="available", match=qm.MatchValue(value=True)))
        return qm.Filter(must=must)

    def _query(self, query, flt: qm.Filter, limit: int, pool: dict) -> list[str]:
        try:
            pts = self.qdrant.query_points(ITEMS_COLLECTION, query=query, query_filter=flt, limit=limit, with_vectors=True, with_payload=True).points
        except Exception as exc:  # e.g. querying by a point id that was removed
            logger.debug("query failed: %s", exc)
            return []
        keys = []
        for p in pts:
            k = (p.payload or {}).get("ck")
            if k:
                if k not in pool:
                    pool[k] = (np.asarray(p.vector, dtype=np.float32), open_payload(p.payload))
                keys.append(k)
        return keys

    def _by_keys(self, keys: list[str], types: list[str], include_unavailable: bool, pool: dict) -> list[str]:
        if not keys:
            return []
        want = {canon_point_id(k): k for k in keys}
        got: dict[str, tuple] = {}
        idl = list(want)
        for i in range(0, len(idl), 256):
            for p in self.qdrant.retrieve(ITEMS_COLLECTION, ids=idl[i : i + 256], with_vectors=True, with_payload=True):
                raw = p.payload or {}
                if raw.get("item_type") in types and (include_unavailable or raw.get("available")):
                    got[want[str(p.id)]] = (np.asarray(p.vector, dtype=np.float32), open_payload(raw))
        for k, v in got.items():
            pool.setdefault(k, v)
        return [k for k in keys if k in got]

    # ---- generation ---------------------------------------------------------------------
    def generate(self, prof: TasteProfile | None, types: list[str], include_unavailable: bool, mood_vec: np.ndarray | None,
                 adj: Adjustment | None = None) -> tuple[dict[str, list[str]], dict]:
        flt = self._filter(types, include_unavailable)
        pool: dict[str, tuple[np.ndarray, dict]] = {}
        lists: dict[str, list[str]] = {}
        if prof is not None:
            quotas = interest_quotas([i.importance for i in prof.interests], total=240)
            for it, q in zip(prof.interests, quotas):
                if q <= 0:
                    continue
                name = "pinned" if it.kind == "pinned" else "interests"
                lists[f"{name}:{it.id}"] = self._query(it.medoid if it.kind == "behaviour" else it.centroid, flt, q, pool)
            if prof.short_term:
                lists["short_term"] = self._query(prof.short_term, flt, 60, pool)
            recent_pos = [a for a in prof.anchors if a.preference > 0.8][:6]
            for a in recent_pos:
                lists[f"item2item:{a.key}"] = self._query(canon_point_id(a.key), flt, 25, pool)
            lists["related"] = self._by_keys([r["key"] for r in prof.related[:200]], types, include_unavailable, pool)
            lists["household"] = self._by_keys([h["key"] for h in prof.household[:120]], types, include_unavailable, pool)
            backlog = sorted(prof.backlog, key=lambda k: -prof.backlog[k])
            lists["backlog"] = self._by_keys(backlog[:100], types, include_unavailable, pool)
            if adj is not None and prof.interests:
                lists["stretch"] = self._stretch(prof, adj, flt, pool)
        if mood_vec is not None:
            lists["mood"] = self._query(mood_vec.tolist(), flt, 120, pool)
        try:
            pts, _ = self.qdrant.scroll(ITEMS_COLLECTION, scroll_filter=flt, limit=80, with_vectors=True, with_payload=True,
                                        order_by=qm.OrderBy(key="quality", direction=qm.Direction.DESC))
        except Exception:
            pts, _ = self.qdrant.scroll(ITEMS_COLLECTION, scroll_filter=flt, limit=80, with_vectors=True, with_payload=True)
        q_keys = []
        for p in pts:
            k = (p.payload or {}).get("ck")
            if k:
                if k not in pool:
                    pool[k] = (np.asarray(p.vector, dtype=np.float32), open_payload(p.payload))
                q_keys.append(k)
        lists["quality"] = q_keys
        return lists, pool

    def _stretch(self, prof: TasteProfile, adj: Adjustment, flt: qm.Filter, pool: dict) -> list[str]:
        """'One step beyond': titles in the band just outside the edge of your strongest
        interests (evolution.stretch_score), best first."""
        found: list[str] = []
        for it in sorted(prof.interests, key=lambda i: -i.importance)[:3]:
            found += self._query(it.centroid, flt, 150, pool)
        keys = list(dict.fromkeys(found))
        if not keys:
            return []
        X = unit(np.stack([pool[k][0] for k in keys]))
        sims = (X @ prof.centroid_matrix().T - prof.baseline) / max(1.0 - prof.baseline, 1e-6)
        radii = np.array([i.radius for i in prof.interests])
        sc = stretch_score(sims, radii, prof.importance_vector(), adj.band)
        return [keys[i] for i in np.argsort(-sc) if sc[i] > 0][:60]

    async def fatigue(self, user_id: str) -> dict[str, int]:
        """Distinct days a title was shown in the last 30 days without being touched since."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT canon_key, count(DISTINCT date_trunc('day', shown_at)) AS days
                FROM impression WHERE user_id = $1 AND shown_at > now() - interval '30 days' AND NOT explore
                GROUP BY canon_key
                """,
                uuid.UUID(user_id),
            )
        return {r["canon_key"]: int(r["days"]) for r in rows}

    # ---- the whole thing ------------------------------------------------------------------
    async def recommend(self, req: Request) -> Result:
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        types = TARGET_TYPES.get(req.item_type, [req.item_type])
        domain = domain_of(types[0])
        overlay = await self.overlay(req.user_id)
        prof = await self.profile(req.user_id, domain)
        adj: Adjustment | None = None
        if prof is not None:
            await self.apply_recent(prof, req.user_id, domain, overlay.get("disabled_sources") or [])
            # Taste evolution: trends, satiation, rediscovery and rhythms shift interest
            # importance for this request (retrieval slots + calibration targets), in memory only.
            strength = float(overlay.get("evolution_strength", 1.0) if overlay.get("evolution_strength") is not None else 1.0)
            adj = adjustment(prof, at=req.at, strength=strength)
            apply_importance(prof, adj)
        timings["profile"] = time.monotonic() - t0

        mood_vec = None
        if req.mood:
            mood_vec = await asyncio.to_thread(lambda: self.embedder().embed_query(req.mood))

        lists, pool = await asyncio.to_thread(self.generate, prof, types, req.include_unavailable, mood_vec, adj)
        timings["generate"] = time.monotonic() - t0
        exclude = set(prof.seen) | set(prof.in_progress) if prof else set()
        fused, sources = rrf(lists)
        cold = prof is None or (not prof.interests and not prof.anchors)
        if prof is None:
            prof = _empty_profile(req.user_id, domain)

        fatigue = await self.fatigue(req.user_id) if not cold else {}
        rel_map = {r["key"]: r for r in prof.related}
        hh_map = {h["key"]: h["support"] for h in prof.household}
        now_ts = datetime.now(timezone.utc).timestamp()
        cands: list[CandidateInput] = []
        for k, (vec, pl) in pool.items():
            if k in exclude:
                continue
            added = pl.get("added_at")
            cands.append(
                CandidateInput(
                    key=k,
                    vector=vec,
                    tags=pl.get("tags") or [],
                    vote_average=pl.get("vote_average"),
                    vote_count=pl.get("vote_count"),
                    added_days_ago=(now_ts - added) / 86400.0 if isinstance(added, (int, float)) else None,
                    related_support=float(rel_map.get(k, {}).get("support", 0.0)),
                    related_via=list(rel_map.get(k, {}).get("via", [])),
                    household_support=float(hh_map.get(k, 0.0)),
                    impressions_unengaged=fatigue.get(k, 0),
                    generators=sources.get(k, set()),
                    rrf=fused.get(k, 0.0),
                    payload={
                        "title": pl.get("title"),
                        "year": pl.get("year"),
                        "collection_id": pl.get("collection_id"),
                        "vote_average": pl.get("vote_average"),
                        "available": pl.get("available"),
                        "library_refs": pl.get("library_refs") or [],
                        "item_type": pl.get("item_type"),
                        "real_key": pl.get("real_key"),
                    },
                )
            )
        anchors = await asyncio.to_thread(self.anchor_vectors, prof) if prof.anchors else {}
        weights = await self.weights()
        scored = score_candidates(prof, cands, anchor_vectors=anchors, weights=weights, mood_vector=mood_vec, evolution=adj)
        timings["score"] = time.monotonic() - t0
        scored.sort(key=lambda s: -s.score)
        explore = req.explore_ratio if req.explore_ratio is not None else float(overlay.get("explore_ratio") or 0.125)
        if cold:
            explore = 0.0
        items = select(scored[:400], prof, req.limit, explore_ratio=explore, seed=daily_seed(req.user_id, f"{req.surface}:{req.item_type}"))
        timings["rerank"] = time.monotonic() - t0

        titles = {k: (pl.get("title") or "") for k, (_, pl) in pool.items()}
        need = [s.nearest_anchor for s in items if s.nearest_anchor and s.nearest_anchor not in titles]
        need += [i.medoid_key for i in prof.interests if i.medoid_key not in titles]
        if need:
            async with self.pool.acquire() as conn:
                for r in await conn.fetch("SELECT canon_key, data FROM canon_item WHERE canon_key = ANY($1::text[])", need):
                    titles[r["canon_key"]] = open_canon_data(r["data"]).get("title") or ""

        request_id = str(uuid.uuid4())
        if req.log and items:
            async with self.pool.acquire() as conn:
                await conn.executemany(
                    "INSERT INTO impression (request_id, user_id, canon_key, surface, position, score, sigma, explore, features) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)",
                    [
                        (uuid.UUID(request_id), uuid.UUID(req.user_id), s.key, req.surface, pos, s.score, s.sigma, s.explore,
                         {k: round(v, 5) for k, v in s.features.items()})
                        for pos, s in enumerate(items)
                    ],
                )
        timings["total"] = time.monotonic() - t0
        return Result(request_id, items, prof, titles, cold_start=cold, timings=timings, pool=scored)

    def describe(self, s: Scored, prof: TasteProfile, titles: dict[str, str]) -> dict:
        refs = s.payload.get("library_refs") or []
        real = s.payload.get("real_key") or ""
        if refs:
            source, sid = refs[0]["source"], refs[0]["source_item_id"]
        elif ":" in real:  # not in the library: e.g. ("tmdb", "movie:603")
            source, sid = real.split(":", 1)
        else:
            source, sid = "muse", s.key
        interest = prof.interests[s.interest_id] if s.interest_id is not None and s.interest_id < len(prof.interests) else None
        return {
            "source": source,
            "source_item_id": sid,
            "canon_key": s.key,  # opaque id: send it back as-is in answers
            "tmdb_id": _tmdb_id(s.payload.get("real_key")),
            "title": s.payload.get("title"),
            "year": s.payload.get("year"),
            "item_type": s.payload.get("item_type"),
            "available": bool(s.payload.get("available")),
            "score": round(s.score, 4),
            "confidence": round(s.confidence, 4),
            "explore": s.explore,
            "interest": (interest.label or ", ".join(t.split(":", 1)[-1] for t in interest.top_tags[:2])) if interest else None,
            "reasons": explain(s, prof, titles),
            # numeric per-feature contributions (kept as a flat map for the Jellyfin plugin)
            "explanation": {k: round(v, 4) for k, v in s.contributions.items() if abs(v) >= 1e-4},
            "generators": s.payload.get("generators", []),
        }


def _tmdb_id(real_key: str | None) -> int | None:
    if real_key and real_key.startswith("tmdb:"):
        try:
            return int(real_key.rsplit(":", 1)[1])
        except ValueError:
            return None
    return None


def _empty_profile(user_id: str, domain: str) -> TasteProfile:
    return TasteProfile(
        user_id=user_id, domain=domain, computed_at=datetime.now(timezone.utc).isoformat(), baseline=0.2, interests=[], negatives=[],
        short_term=None, tag_weights={}, tag_scale=1.0, anchors=[], seen=[], in_progress=[], backlog={}, abandoned=[],
        n_positive=0, n_negative=0, total_confidence=0.0,
    )


def pool_vectors(pool: dict) -> tuple[list[str], np.ndarray]:
    keys = list(pool)
    return keys, unit(np.stack([pool[k][0] for k in keys])) if keys else np.zeros((0, 1))
