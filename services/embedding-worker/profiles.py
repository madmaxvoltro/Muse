"""Builds and stores taste profiles: events -> canonical per-title signals -> per-domain
multi-interest profiles (muse_taste.profile), plus the precomputed candidate lists the API
fuses at request time (TMDB-related support, household CF)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import asyncpg
import numpy as np
from muse_schema import canon_point_id
from muse_taste import llm as llm_mod
from muse_taste.canon import domain_of
from muse_taste.profile import TasteProfile, build_profile, estimate_baseline
from muse_taste.retrieval import household_support, related_support
from muse_taste.signals import ItemSignal, SignalEvent, aggregate, from_rows
from muse_taste.store import ITEMS_COLLECTION
from qdrant_client import QdrantClient

logger = logging.getLogger("muse.worker.profiles")

LOOKBACK_DAYS = int(os.environ.get("MUSE_TASTE_LOOKBACK_DAYS", "3650"))
PINNED_DOMAINS = ("video", "youtube")

EVENTS_SQL = """
SELECT e.source, e.source_item_id, e.item_type, e.action, e.action_weight, e."timestamp", e.progress_pct, e.metadata,
       i.canon_key, i.parent_key, p.canon_key AS parent_canon
FROM event e
LEFT JOIN item i ON i.item_key = e.source || ':' || e.source_item_id
LEFT JOIN item p ON p.item_key = i.parent_key
WHERE e.user_id = $1 AND e.source_item_id IS NOT NULL AND e.action <> 'cataloged'
  AND e."timestamp" > now() - make_interval(days => $2::int)
"""


def to_signal_events(rows: list) -> list[SignalEvent]:
    return from_rows(rows)


def fetch_vectors(qdrant: QdrantClient, keys: list[str]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    ids = {canon_point_id(k): k for k in keys}
    id_list = list(ids)
    for i in range(0, len(id_list), 256):
        for p in qdrant.retrieve(ITEMS_COLLECTION, ids=id_list[i : i + 256], with_vectors=True, with_payload=False):
            out[ids[str(p.id)]] = np.asarray(p.vector, dtype=np.float32)
    return out


class DomainStats:
    """Per-cycle cache: tag document frequencies, catalog size, embedding baseline."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[dict[str, int], int, float]] = {}

    async def get(self, conn: asyncpg.Connection, qdrant: QdrantClient, domain: str) -> tuple[dict[str, int], int, float]:
        if domain not in self._cache:
            rows = await conn.fetch("SELECT t, count(*) AS n FROM canon_item, unnest(tags) t WHERE domain = $1 GROUP BY t", domain)
            n = await conn.fetchval("SELECT count(*) FROM canon_item WHERE domain = $1", domain)
            sample = await conn.fetch("SELECT canon_key FROM canon_item WHERE domain = $1 AND embedded_hash IS NOT NULL ORDER BY random() LIMIT 800", domain)
            vecs = fetch_vectors(qdrant, [r["canon_key"] for r in sample])
            base = estimate_baseline(np.stack(list(vecs.values()))) if len(vecs) >= 20 else 0.2
            self._cache[domain] = ({r["t"]: r["n"] for r in rows}, int(n or 0), base)
        return self._cache[domain]


async def load_overlay(conn: asyncpg.Connection, user_id: str) -> dict:
    row = await conn.fetchrow("SELECT source_weights, disabled_sources, pinned_topics FROM user_overlay WHERE user_id = $1", uuid.UUID(user_id))
    if not row:
        return {"source_weights": {}, "disabled_sources": [], "pinned_topics": []}
    return {k: (row[k] or ({} if k == "source_weights" else [])) for k in ("source_weights", "disabled_sources", "pinned_topics")}


async def user_signals(conn: asyncpg.Connection, user_id: str, now: datetime) -> tuple[dict[str, ItemSignal], dict]:
    overlay = await load_overlay(conn, user_id)
    rows = await conn.fetch(EVENTS_SQL, uuid.UUID(user_id), LOOKBACK_DAYS)
    sig = aggregate(to_signal_events(rows), now=now, source_weights=overlay["source_weights"], disabled_sources=overlay["disabled_sources"])
    return sig, overlay


async def recompute_users(
    pool: asyncpg.Pool,
    qdrant: QdrantClient,
    user_ids: set[str],
    embedder=None,
    llm: llm_mod.LLM | None = None,
    model_name: str | None = None,
) -> int:
    if not user_ids:
        return 0
    now = datetime.now(timezone.utc)
    stats = DomainStats()
    # Signals for everyone first: household CF needs the others' judgements.
    all_signals: dict[str, dict[str, ItemSignal]] = {}
    overlays: dict[str, dict] = {}
    async with pool.acquire() as conn:
        everyone = [str(r["user_id"]) for r in await conn.fetch("SELECT DISTINCT user_id FROM event WHERE action <> 'cataloged'")]
        for uid in set(everyone) | user_ids:
            all_signals[uid], overlays[uid] = await user_signals(conn, uid, now)
        old_profiles = {
            (str(r["user_id"]), r["domain"]): r["profile"]
            for r in await conn.fetch("SELECT user_id, domain, profile FROM taste_profile WHERE user_id = ANY($1::uuid[])", [uuid.UUID(u) for u in user_ids])
        }

    pinned_vecs: dict[str, np.ndarray] = {}
    count = 0
    for uid in user_ids:
        sig = all_signals.get(uid) or {}
        by_domain: dict[str, dict[str, ItemSignal]] = defaultdict(dict)
        for k, s in sig.items():
            by_domain[domain_of(s.item_type)][k] = s
        pinned_topics = [t for t in overlays[uid]["pinned_topics"] if isinstance(t, str) and t.strip()]
        if pinned_topics and "video" not in by_domain:
            by_domain["video"] = {}
        for domain, dsig in by_domain.items():
            if domain == "search_query" or domain == "chat":
                continue
            async with pool.acquire() as conn:
                tag_df, n_catalog, baseline = await stats.get(conn, qdrant, domain)
                keys = list(dsig)
                tag_rows = await conn.fetch("SELECT canon_key, tags, title, related FROM canon_item WHERE canon_key = ANY($1::text[])", keys)
            tags = {r["canon_key"]: list(r["tags"]) for r in tag_rows}
            titles = {r["canon_key"]: r["title"] for r in tag_rows}
            related_of = {r["canon_key"]: list(r["related"]) for r in tag_rows}
            vectors = await asyncio.to_thread(fetch_vectors, qdrant, keys)
            pinned = []
            if embedder is not None and domain in PINNED_DOMAINS:
                for t in pinned_topics[:5]:
                    if t not in pinned_vecs:
                        pinned_vecs[t] = await asyncio.to_thread(embedder.embed_query, t)
                    pinned.append((t, pinned_vecs[t]))
            if not vectors and not pinned:
                continue
            prof = build_profile(uid, domain, dsig, vectors, tags, tag_df, max(n_catalog, 1), baseline=baseline, now=now, pinned=pinned, model=model_name)

            top = [(a.key, a.weight, titles.get(a.key, "")) for a in prof.anchors if a.weight > 0][:30]
            rel = related_support(top, related_of)
            prof.related = [
                {"key": k, "support": round(v[0], 4), "via": v[1]}
                for k, v in sorted(rel.items(), key=lambda kv: -kv[1][0])
                if not (k in dsig and dsig[k].seen)
            ][:250]
            mine = {k: s.preference for k, s in dsig.items() if s.explicit or s.seen}
            others = {
                o: {k: s.preference for k, s in osig.items() if domain_of(s.item_type) == domain and (s.explicit or s.seen)}
                for o, osig in all_signals.items()
                if o != uid
            }
            hh = household_support(mine, others)
            prof.household = [{"key": k, "support": round(v, 4)} for k, v in sorted(hh.items(), key=lambda kv: -kv[1])[:150]]

            old = old_profiles.get((uid, domain))
            await _llm_touches(prof, old, llm, titles, pool)

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO taste_profile (user_id, domain, profile, computed_at) VALUES ($1, $2, $3::jsonb, now())
                    ON CONFLICT (user_id, domain) DO UPDATE SET profile = EXCLUDED.profile, computed_at = now()
                    """,
                    uuid.UUID(uid),
                    domain,
                    prof.to_json(),
                )
            count += 1
            logger.info(
                "profile user=%s domain=%s: %d interests, %d+/%d- titles, %d related, %d household",
                uid, domain, len(prof.interests), prof.n_positive, prof.n_negative, len(prof.related), len(prof.household),
            )
    return count


def _members_hash(members: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(members)).encode()).hexdigest()[:12]


async def _llm_touches(prof: TasteProfile, old: dict | None, llm, titles: dict[str, str], pool: asyncpg.Pool) -> None:
    """Interest labels and taste summary: reuse previous results when the interest's members
    didn't change; only call the LLM for new or changed interests."""
    old_labels: dict[str, str] = {}
    if old:
        for i in old.get("interests", []):
            if i.get("label") and i.get("kind") == "behaviour":
                old_labels[_members_hash(i.get("members", []))] = i["label"]
    personal_ok = llm_mod.personal_tasks_allowed(llm)
    need_titles = [k for i in prof.interests for k in i.members if k not in titles]
    if need_titles:
        async with pool.acquire() as conn:
            for r in await conn.fetch("SELECT canon_key, title FROM canon_item WHERE canon_key = ANY($1::text[])", need_titles):
                titles[r["canon_key"]] = r["title"]
    changed = False
    for it in prof.interests:
        if it.kind != "behaviour":
            continue
        h = _members_hash(it.members)
        if h in old_labels:
            it.label = old_labels[h]
        elif personal_ok and len(it.members) >= 2:
            try:
                it.label = await asyncio.to_thread(llm_mod.label_interest, llm, [titles.get(m, m) for m in it.members], it.top_tags)
                changed = True
            except Exception as exc:
                logger.warning("interest label failed: %s", exc)
    if old and not changed and old.get("summary"):
        prof.summary = old["summary"]
    elif personal_ok and prof.interests:
        loved = [titles.get(a.key, "") for a in prof.anchors if a.preference >= 1.5][:12]
        disliked = [titles.get(a.key, "") for a in prof.anchors if a.preference < 0][:8]
        try:
            prof.summary = await asyncio.to_thread(
                llm_mod.summarize_taste, llm, [(i.label or ", ".join(i.top_tags[:2]), i.importance) for i in prof.interests], [t for t in loved if t], [t for t in disliked if t]
            )
        except Exception as exc:
            logger.warning("taste summary failed: %s", exc)
