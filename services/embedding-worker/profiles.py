"""Builds and stores taste profiles: events -> canonical per-title signals -> per-domain
multi-interest profiles (muse_taste.profile), plus the precomputed candidate lists the API
fuses at request time (TMDB-related support, household CF)."""

from __future__ import annotations

import asyncio
import hashlib
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
from muse_taste.evolution import analyze
from muse_taste.learn import label_impression
from muse_taste.profile import TasteProfile, build_profile, estimate_baseline
from muse_taste.retrieval import household_support, related_support
from muse_taste.reconsume import candidates as reconsume_candidates
from muse_taste.signals import ItemSignal, SignalEvent, aggregate, feedback_overrides, from_rows
from muse_schema.crypto import P_OVERLAY, P_PROFILE, get_vault
from muse_taste.store import ITEMS_COLLECTION, open_canon_data
from qdrant_client import QdrantClient

import questions_producer

logger = logging.getLogger("muse.worker.profiles")

LOOKBACK_DAYS = int(os.environ.get("MUSE_TASTE_LOOKBACK_DAYS", "3650"))
PINNED_DOMAINS = ("video", "youtube")
TIMEZONE = os.environ.get("MUSE_TIMEZONE", "UTC")  # for rhythms: "evening" means your evening

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


async def canon_data(conn: asyncpg.Connection, keys: list[str]) -> dict[str, dict]:
    """Opened (decrypted) canon_item data for blind keys."""
    if not keys:
        return {}
    rows = await conn.fetch("SELECT canon_key, data FROM canon_item WHERE canon_key = ANY($1::text[])", keys)
    return {r["canon_key"]: open_canon_data(r["data"]) for r in rows}


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
            # tags are sealed: count them here, not in SQL
            df: dict[str, int] = {}
            n = 0
            for r in await conn.fetch("SELECT data FROM canon_item WHERE domain = $1", domain):
                n += 1
                for t in open_canon_data(r["data"]).get("tags") or []:
                    df[t] = df.get(t, 0) + 1
            sample = await conn.fetch("SELECT canon_key FROM canon_item WHERE domain = $1 AND embedded_hash IS NOT NULL ORDER BY random() LIMIT 800", domain)
            vecs = fetch_vectors(qdrant, [r["canon_key"] for r in sample])
            base = estimate_baseline(np.stack(list(vecs.values()))) if len(vecs) >= 20 else 0.2
            self._cache[domain] = (df, n, base)
        return self._cache[domain]


async def load_overlay(conn: asyncpg.Connection, user_id: str) -> dict:
    row = await conn.fetchrow("SELECT source_weights, disabled_sources, pinned_topics FROM user_overlay WHERE user_id = $1", uuid.UUID(user_id))
    if not row:
        return {"source_weights": {}, "disabled_sources": [], "pinned_topics": []}
    out = {k: (row[k] or ({} if k == "source_weights" else [])) for k in ("source_weights", "disabled_sources", "pinned_topics")}
    out["pinned_topics"] = get_vault().open_json(out["pinned_topics"], P_OVERLAY) or []  # sealed: free text about you
    return out


async def user_signals(conn: asyncpg.Connection, user_id: str, now: datetime) -> tuple[dict[str, ItemSignal], dict, list[SignalEvent]]:
    overlay = await load_overlay(conn, user_id)
    rows = await conn.fetch(EVENTS_SQL, uuid.UUID(user_id), LOOKBACK_DAYS)
    events = [e for e in to_signal_events(rows) if e.source not in set(overlay["disabled_sources"])]
    sig = aggregate(events, now=now, source_weights=overlay["source_weights"])
    return sig, overlay, events


async def stretch_stats(conn: asyncpg.Connection, user_id: str, events: list[SignalEvent]) -> tuple[int, int]:
    """How often 'one step beyond' suggestions were shown and taken (drives the stretch band)."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (canon_key) canon_key, shown_at FROM impression
        WHERE user_id = $1 AND (features->>'stretch')::float > 0.2
          AND shown_at > now() - interval '120 days' AND shown_at < now() - interval '2 days'
        ORDER BY canon_key, shown_at
        """,
        uuid.UUID(user_id),
    )
    by_key: dict[str, list[SignalEvent]] = defaultdict(list)
    for e in events:
        by_key[e.key].append(e)
    accepted = sum(1 for r in rows if label_impression(r["shown_at"], by_key.get(r["canon_key"], []))[0] > 0.5)
    return len(rows), accepted


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
    all_events: dict[str, list[SignalEvent]] = {}
    async with pool.acquire() as conn:
        everyone = [str(r["user_id"]) for r in await conn.fetch("SELECT DISTINCT user_id FROM event WHERE action <> 'cataloged'")]
        for uid in set(everyone) | user_ids:
            all_signals[uid], overlays[uid], all_events[uid] = await user_signals(conn, uid, now)
        old_profiles = {
            (str(r["user_id"]), r["domain"]): get_vault().open_json(r["profile"], P_PROFILE)
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
                cdata = await canon_data(conn, keys)
            tags = {k: list(d.get("tags") or []) for k, d in cdata.items()}
            titles = {k: d.get("title") or "" for k, d in cdata.items()}
            related_of = {k: list(d.get("related") or []) for k, d in cdata.items()}
            vectors = await asyncio.to_thread(fetch_vectors, qdrant, keys)
            pinned = []
            if embedder is not None and domain in PINNED_DOMAINS:
                for t in pinned_topics[:5]:
                    if t not in pinned_vecs:
                        pinned_vecs[t] = await asyncio.to_thread(embedder.embed_query, t)
                    pinned.append((t, pinned_vecs[t]))
            if not vectors and not pinned:
                continue
            devents = [e for e in all_events.get(uid, []) if domain_of(e.item_type) == domain]
            muted, tag_overrides = feedback_overrides(devents)  # your corrections to Muse's reasons
            prof = build_profile(uid, domain, dsig, vectors, tags, tag_df, max(n_catalog, 1), baseline=baseline, now=now, pinned=pinned,
                                 model=model_name, muted=muted, tag_overrides=tag_overrides)
            prof.reconsume = reconsume_candidates(domain, dsig, devents, now=now)  # worth watching / hearing again

            top = [(a.key, a.weight, titles.get(a.key, "")) for a in prof.anchors if a.weight > 0][:30]
            rel = related_support(top, related_of)
            prof.related = [
                {"key": k, "support": round(v[0], 4), "via": v[1], "via_keys": v[2]}
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

            # taste evolution: lifecycle, satiation, rediscovery, rhythms, stretch, timeline
            async with pool.acquire() as conn:
                st_stats = await stretch_stats(conn, uid, devents)
            prof.evolution = analyze(prof, devents, vectors, now=now, tz=TIMEZONE, stretch_stats=st_stats)

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
                    get_vault().seal_json(prof.to_json(), P_PROFILE),
                )
            count += 1
            # Optional question queue: never let it affect profiles (see questions_producer).
            try:
                await questions_producer.produce(pool, qdrant, prof, titles, vectors)
            except Exception:
                logger.exception("question queue update failed for user=%s domain=%s (ignored)", uid, domain)
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
            for k, d in (await canon_data(conn, need_titles)).items():
                titles[k] = d.get("title") or ""
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
    old_ev = (old or {}).get("evolution") or {}
    milestones_now = [m["text"] for m in prof.evolution.get("milestones", [])]
    if old_ev.get("narrative") and [m["text"] for m in old_ev.get("milestones", [])] == milestones_now:
        prof.evolution["narrative"] = old_ev["narrative"]
    elif personal_ok and milestones_now:
        try:
            prof.evolution["narrative"] = await asyncio.to_thread(llm_mod.narrate_evolution, llm, prof, titles)
        except Exception as exc:
            logger.warning("evolution narrative failed: %s", exc)
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
