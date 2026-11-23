"""Recommendations for several people at once ("what do we watch?").

Each member is scored with their own taste (all spaces, facets, facts, preferences); the group
score mixes the average with the least-happy member (least misery), so nobody gets steamrolled.
On top:
* vetoes — anything a member said to avoid, or rated down, is out;
* seen — by default only things *everyone* has already seen are dropped (seen_by is reported);
* fairness — members whose fit was low in this group's recent picks get more weight now, so
  over time everyone gets their turn.
Any number of groups can be asked for at the same time; nothing here is global state.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict

from muse_schema import identity_group
from muse_schema.storage import fetch_open

from . import multispace as E
from .context import Ctx
from .scoring import apply_knowledge


async def fairness_weights(conn, members: list[uuid.UUID]) -> dict[uuid.UUID, float]:
    """1 + catch-up: members with lower average fit over this group's last picks weigh more."""
    try:
        rows = await conn.fetch(
            "SELECT fits FROM group_pick WHERE members @> $1::uuid[] AND members <@ $1::uuid[] ORDER BY picked_at DESC LIMIT 10",
            members)
    except Exception:
        rows = []
    fits: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        f = json.loads(r["fits"]) if isinstance(r["fits"], str) else (r["fits"] or {})
        for uid, v in f.items():
            fits[uid].append(float(v))
    avg = {m: (sum(fits[str(m)]) / len(fits[str(m)]) if fits[str(m)] else None) for m in members}
    known = [v for v in avg.values() if v is not None]
    if not known:
        return {m: 1.0 for m in members}
    mean = sum(known) / len(known)
    return {m: 1.0 + max(0.0, min(0.6, (mean - avg[m]) * 2)) if avg[m] is not None else 1.0 for m in members}


async def group_recommend(pool, qd, member_ids: list[str], item_type: str, limit: int, ctx_for, *,
                          seen: str = "all", sources=None, explore_ratio: float = 0.1, load_knowledge=None) -> dict:
    async with pool.acquire() as conn:
        canon: list[uuid.UUID] = []
        groups: dict[uuid.UUID, list[uuid.UUID]] = {}
        for m in member_ids:
            c, g = await identity_group(conn, m)
            if c not in groups:
                canon.append(c)
                groups[c] = g
        persons: dict[uuid.UUID, E.Person] = {}
        ctxs: dict[uuid.UUID, Ctx] = {}
        for c in canon:
            facts, overlay = await load_knowledge(pool, c)
            ctxs[c] = await ctx_for(conn, c)
            persons[c] = await E.load_person(conn, qd, c, groups[c], item_type, ctxs[c],
                                             facts=[f for f in facts if f["domain"] in ("general", item_type)], overlay=overlay)
        owned = {c: set(p.owned) for c, p in persons.items()}
        all_seen = set.intersection(*owned.values()) if owned else set()
        any_seen = set.union(*owned.values()) if owned else set()
        drop = {"all": all_seen, "any": any_seen, "none": set()}.get(seen, all_seen)

        # Candidates: a blended "group taste" plus every member's own generators.
        found: dict[str, set[str]] = defaultdict(set)
        mix = E.Person(canon[0], groups[canon[0]], item_type)
        mix.taste = E.blend([(p.taste, 1.0) for p in persons.values()])
        mix.audio_taste = E.blend([(p.audio_taste, 1.0) for p in persons.values()])
        mix.behavior_taste = E.blend([(p.behavior_taste, 1.0) for p in persons.values()])
        for who in [mix, *persons.values()]:
            saved = who.owned
            who.owned = drop
            for k, gens in (await E.generate(conn, qd, who, ctxs[canon[0]], sources=sources)).items():
                found[k] |= gens
            who.owned = saved
        found = dict(sorted(found.items(), key=lambda kv: -len(kv[1]))[:E.POOL])

        per_member: dict[uuid.UUID, dict[str, E.Item]] = {}
        for c, p in persons.items():
            items = E.score(await E.featurize(conn, qd, p, ctxs[c], found))
            kept = apply_knowledge(items, p.facts, p.overlay.get("source_weights") or {})
            per_member[c] = {it.key: it for it in kept}
        fair = await fairness_weights(conn, canon)

    # Vetoes: filtered out by any member's facts, or rated/judged negatively by any member.
    keys = set.intersection(*(set(d) for d in per_member.values())) if per_member else set()
    negative: set[str] = set()
    async with pool.acquire() as conn:
        for c in canon:
            negative |= {f"{r['source']}:{r['source_item_id']}" for r in await fetch_open(
                conn,
                "SELECT source, source_item_id, metadata FROM event WHERE user_id = ANY($1::uuid[]) AND item_type = $2 "
                "AND action IN ('rejected', 'rated', 'skipped', 'unfavorited') AND action_weight < 0", groups[c], item_type)}
    keys -= negative

    top = {c: max((it.score for it in per_member[c].values()), default=1.0) or 1.0 for c in canon}
    results: list[E.Item] = []
    for k in keys:
        fits = {c: max(0.0, per_member[c][k].score / top[c]) for c in canon}
        wsum = sum(fair[c] for c in canon)
        mean = sum(fits[c] * fair[c] for c in canon) / wsum
        least = min(fits.values())
        base = per_member[canon[0]][k]
        it = E.Item(source=base.source, source_item_id=base.source_item_id, item_type=item_type, similarity=base.similarity,
                    recency_of_similar_days=base.recency_of_similar_days, within_user_popularity=base.within_user_popularity,
                    completion_rate=base.completion_rate, confidence=sum(per_member[c][k].confidence for c in canon) / len(canon),
                    genres=base.genres, title=base.title)
        it.generators, it.payload, it.artist = base.generators, base.payload, base.artist
        it.features = {"group_mean": mean, "least_misery": least}
        it.reasons = {"group_mean": 0.5 * mean, "least_misery": 0.5 * least}
        it.score = 0.5 * mean + 0.5 * least
        it.member_fits = {str(c): round(v, 3) for c, v in fits.items()}  # type: ignore[attr-defined]
        it.seen_by = [str(c) for c in canon if k in owned[c]]  # type: ignore[attr-defined]
        results.append(it)
    final = E.rerank(results, limit, explore_ratio, [])
    return {"members": [str(c) for c in canon], "fairness": {str(c): round(v, 3) for c, v in fair.items()},
            "items": final, "vetoed": len(negative & set().union(*(set(d) for d in per_member.values())))}


async def member_fits(pool, qd, member_ids: list[str], source: str, source_item_id: str, item_type: str, ctx_for, load_knowledge) -> dict[str, float]:
    """How well one item fits each member (0..1, relative to their best current candidates)."""
    key = f"{source}:{source_item_id}"
    out: dict[str, float] = {}
    async with pool.acquire() as conn:
        for m in member_ids:
            c, g = await identity_group(conn, m)
            facts, overlay = await load_knowledge(pool, c)
            ctx = await ctx_for(conn, c)
            p = await E.load_person(conn, qd, c, g, item_type, ctx, facts=facts, overlay=overlay)
            p.owned = set()
            found = await E.generate(conn, qd, p, ctx)
            found.setdefault(key, set()).add("asked")
            items = {it.key: it for it in E.score(await E.featurize(conn, qd, p, ctx, found))}
            best = max((it.score for it in items.values()), default=1.0) or 1.0
            out[str(c)] = round(max(0.0, items[key].score / best), 3) if key in items else 0.0
    return out
