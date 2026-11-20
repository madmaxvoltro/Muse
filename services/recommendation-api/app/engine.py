"""The recommendation engine: one pipeline for lists, groups, next-track and radio.

1. **Person** — everything Muse knows about one person for one domain: taste vectors in three
   spaces (text/metadata, sound via CLAP, behaviour via sessions), taste facets, a context
   taste for the current situation, their preference model over audio features, facts, sliders,
   what they've already seen, what they keep ignoring, active goals.
2. **Generators** — candidates from many angles, so the list isn't one neighbourhood of one
   average: taste, each facet, sound, behaviour, co-occurrence (played after what you played),
   new arrivals, the current mood, goals.
3. **Features** — for every candidate, its fit in every space (not only the one that found it),
   its preference-model probability, mood fit, context fit, and the classic genre statistics.
4. **Score** — hand-weighted (or learned, when a trained ranker beats them on holdout), plus
   what Muse was told (facts), source weights, a penalty for things shown again and again
   without reaction, a goal boost.
5. **Re-rank** — facet quotas (each of your tastes gets room), artist and genre caps, explore
   slots for uncertain-but-promising items.
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from muse_schema import content_point_id, taste_point_id
from muse_schema import moods as M
from qdrant_client.http.models import FieldCondition, Filter, MatchAny, MatchValue

from .candidates import genre_features, get_owned_item_ids, get_user_genre_stats, resolve_taste_vector
from .context import Ctx
from .scoring import Candidate, confidence

POOL = 400  # candidates kept before scoring

# Hand weights. Missing features contribute nothing. Every term ends up in `explanation`.
WEIGHTS = {
    "similarity": 0.28, "facet_fit": 0.14, "audio_sim": 0.14, "behavior_sim": 0.08, "cooccur": 0.08,
    "preference": 0.08, "context_fit": 0.05, "mood_fit": 0.16, "recency_of_similar": 0.05,
    "within_user_popularity": 0.04, "completion_rate": 0.04, "confidence": 0.05, "freshness": 0.03, "goal_fit": 0.08,
}
FEATURES = tuple(WEIGHTS)


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def cos(a, b) -> float | None:
    if a is None or b is None:
        return None
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def blend(vectors: list[tuple[list[float] | None, float]]) -> list[float] | None:
    """Weighted mix of normalised vectors (None entries skipped)."""
    present = [(v, w) for v, w in vectors if v is not None and w > 0]
    if not present:
        return None
    dim = len(present[0][0])
    out = [0.0] * dim
    for v, w in present:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        for i, x in enumerate(v):
            out[i] += w * x / n
    return out


@dataclass
class Facet:
    space: str
    idx: int
    share: float
    label: str
    vector: list[float]


@dataclass
class Person:
    canonical: uuid.UUID
    group: list[uuid.UUID]
    item_type: str
    taste: list[float] | None = None
    taste_how: str = "none"
    context_taste: list[float] | None = None
    audio_taste: list[float] | None = None
    behavior_taste: list[float] | None = None
    facets: list[Facet] = field(default_factory=list)
    pref_model: dict | None = None
    pref_summary: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    overlay: dict = field(default_factory=dict)
    owned: set[str] = field(default_factory=set)
    genre_stats: dict = field(default_factory=dict)
    ignored: dict[str, int] = field(default_factory=dict)
    recent_positive: list[str] = field(default_factory=list)
    goals: list[dict] = field(default_factory=list)


@dataclass
class Item(Candidate):
    generators: set[str] = field(default_factory=set)
    features: dict[str, float] = field(default_factory=dict)
    audio: dict = field(default_factory=dict)
    facet: str | None = None
    facet_key: str | None = None
    artist: str = ""
    payload: dict = field(default_factory=dict)
    goal: str | None = None
    tv: list[float] | None = None  # text / sound / behaviour vectors, for sequencing
    av: list[float] | None = None
    bv: list[float] | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_item_id}"


# --- loading a person ----------------------------------------------------------------------------


def _scroll_facets(qd, collection: str, user: str, item_type: str, space: str) -> list[Facet]:
    try:
        pts, _ = qd.scroll(collection, scroll_filter=Filter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=user)),
            FieldCondition(key="item_type", match=MatchValue(value=item_type))]), with_vectors=True, limit=20)
    except Exception:
        return []
    return [Facet(space, int((p.payload or {}).get("idx", 0)), float((p.payload or {}).get("share", 0)),
                  (p.payload or {}).get("label", ""), list(p.vector)) for p in pts]


def _point(qd, collection: str, pid: str) -> list[float] | None:
    try:
        pts = qd.retrieve(collection, [pid], with_vectors=True)
    except Exception:
        return None
    return list(pts[0].vector) if pts else None


async def load_person(conn, qd, canonical: uuid.UUID, group: list[uuid.UUID], item_type: str, ctx: Ctx, *,
                      facts: list[dict], overlay: dict, taste_from: str | None = None, blend_ratio: float = 0.0) -> Person:
    user = str(canonical)
    p = Person(canonical, group, item_type, facts=facts, overlay=overlay)
    p.taste, p.taste_how = resolve_taste_vector(qd, user, item_type, taste_from, blend_ratio)
    ctx_vecs = [_point(qd, "taste_vectors", taste_point_id(user, f"{item_type}@{b}")) for b in ctx.buckets]
    p.context_taste = blend([(v, 1.0) for v in ctx_vecs])
    p.audio_taste = _point(qd, "audio_taste", taste_point_id(user, item_type))
    p.behavior_taste = _point(qd, "behavior_taste", taste_point_id(user, item_type))
    p.facets = _scroll_facets(qd, "taste_facets", user, item_type, "text") + _scroll_facets(qd, "taste_facets_audio", user, item_type, "audio")
    p.owned = await get_owned_item_ids(conn, group)
    p.genre_stats = await get_user_genre_stats(conn, qd, group)
    try:
        row = await conn.fetchrow("SELECT model FROM taste_model WHERE user_id = $1 AND item_type = $2 AND kind = 'audio_pref'", canonical, item_type)
        if row:
            p.pref_model = _j(row["model"])
            p.pref_summary = p.pref_model.get("summary", [])
        p.ignored = {f"{r['source']}:{r['source_item_id']}": r["n"] for r in await conn.fetch(
            "SELECT source, source_item_id, count(*) AS n FROM impression WHERE user_id = $1 AND item_type = $2 "
            "AND shown_at > now() - interval '30 days' AND outcome IS DISTINCT FROM 'positive' GROUP BY 1, 2 HAVING count(*) >= 3",
            canonical, item_type)}
        p.goals = [dict(r, criteria=_j(r["criteria"])) for r in await conn.fetch(
            "SELECT id, title, domain, criteria FROM goal WHERE user_id = $1 AND status = 'active' AND domain IN ($2, 'general')",
            canonical, item_type)]
    except Exception:  # database without migrations 008/009
        pass
    p.recent_positive = [f"{r['source']}:{r['source_item_id']}" for r in await conn.fetch(
        "SELECT source, source_item_id FROM event WHERE user_id = ANY($1::uuid[]) AND item_type = $2 AND action_weight > 0 "
        "AND source_item_id IS NOT NULL AND action <> 'cataloged' ORDER BY \"timestamp\" DESC LIMIT 25", group, item_type)]
    return p


# --- generators ---------------------------------------------------------------------------------------


def _filter(item_type: str | None, sources: list[str] | None, exclude: list[str] | None) -> Filter:
    must = [FieldCondition(key="item_type", match=MatchValue(value=item_type))] if item_type else []
    if sources:
        must.append(FieldCondition(key="source", match=MatchAny(any=sources)))
    must_not = [FieldCondition(key="source", match=MatchAny(any=exclude))] if exclude else None
    return Filter(must=must, must_not=must_not)


def _query(qd, collection: str, vector, flt: Filter | None, limit: int) -> list[tuple[str, str, float, dict]]:
    if vector is None or limit <= 0:
        return []
    try:
        hits = qd.query_points(collection_name=collection, query=list(vector), query_filter=flt, limit=limit).points
    except Exception:
        return []
    return [((h.payload or {}).get("source", ""), (h.payload or {}).get("source_item_id", ""), float(h.score), h.payload or {}) for h in hits]


async def generate(conn, qd, p: Person, ctx: Ctx, *, sources=None, exclude_sources=None, exclude: set[str] | None = None,
                   seeds: list[tuple[str, str]] | None = None) -> dict[str, set[str]]:
    """key -> generators that proposed it."""
    found: dict[str, set[str]] = defaultdict(set)
    t = p.item_type
    flt = _filter(t, sources, exclude_sources)
    audio_flt = _filter(None, sources, exclude_sources)  # audio_items holds tracks only

    def add(hits, name):
        for s, sid, _, _ in hits:
            if s and sid:
                found[f"{s}:{sid}"].add(name)

    mood_text = ctx.mood_text_vec
    taste = blend([(p.taste, 1.0), (p.context_taste, 0.35), (mood_text, 0.45 if mood_text else 0)]) if p.taste or mood_text else None
    add(_query(qd, "content_items", taste, flt, 160), "taste")
    for f in p.facets:
        coll, fl = ("content_items", flt) if f.space == "text" else ("audio_items", audio_flt)
        add(_query(qd, coll, blend([(f.vector, 1.0), (mood_text if f.space == "text" else ctx.mood_audio_vec, 0.3)]), fl,
                   max(12, int(120 * f.share))), f"facet:{f.space}:{f.idx}")
    if t == "track":
        add(_query(qd, "audio_items", blend([(p.audio_taste, 1.0), (ctx.mood_audio_vec, 0.5 if ctx.mood_audio_vec else 0)]), audio_flt, 100), "sound")
        if ctx.mood_audio_vec:
            add(_query(qd, "audio_items", ctx.mood_audio_vec, audio_flt, 50), "mood")
    if mood_text:
        add(_query(qd, "content_items", mood_text, flt, 50), "mood")
    add(_query(qd, "behavior_items", p.behavior_taste, Filter(must=[FieldCondition(key="source", match=MatchAny(any=sources))]) if sources else None, 60), "behavior")
    for s, sid in seeds or []:  # "more like this" / next track
        pid = content_point_id(s, sid)
        for coll, name, fl in (("content_items", "seed_text", flt), ("audio_items", "seed_sound", audio_flt), ("behavior_items", "seed_behavior", None)):
            vec = _point(qd, coll, pid)
            add(_query(qd, coll, vec, fl, 60), name)
    recent = [f"{s}:{sid}" for s, sid in (seeds or [])] + p.recent_positive[:15]
    try:
        for r in await conn.fetch(
            "SELECT to_key, sum(count + 2 * completions - 2 * skips) AS s FROM item_transition WHERE from_key = ANY($1::text[]) "
            "GROUP BY 1 HAVING sum(count + 2 * completions - 2 * skips) > 0 ORDER BY 2 DESC LIMIT 80", recent):
            found[r["to_key"]].add("cooccur")
    except Exception:
        pass
    for r in await conn.fetch(
        "SELECT DISTINCT source, source_item_id FROM event WHERE action = 'cataloged' AND item_type = $1 AND source_item_id IS NOT NULL "
        "AND ingested_at > now() - interval '14 days' AND source <> 'catalog' AND ($2::text[] IS NULL OR source = ANY($2)) LIMIT 150",
        t, sources):
        found[f"{r['source']}:{r['source_item_id']}"].add("fresh")
    for g in p.goals:
        add(_query(qd, "content_items", (g["criteria"] or {}).get("vec"), flt, 40), f"goal:{g['id']}")

    drop = set(p.owned) | (exclude or set())
    out = {k: v for k, v in found.items() if k not in drop}
    if exclude_sources:
        out = {k: v for k, v in out.items() if k.split(":", 1)[0] not in exclude_sources}
    if sources:
        out = {k: v for k, v in out.items() if k.split(":", 1)[0] in sources}
    # Keep the pool bounded: items proposed by more generators first.
    return dict(sorted(out.items(), key=lambda kv: -len(kv[1]))[:POOL])


# --- features ------------------------------------------------------------------------------------------


def _retrieve_many(qd, collection: str, ids: list[str], with_vectors=True) -> dict[str, Any]:
    out = {}
    for i in range(0, len(ids), 256):
        try:
            for pt in qd.retrieve(collection, ids[i:i + 256], with_vectors=with_vectors, with_payload=True):
                out[str(pt.id)] = pt
        except Exception:
            break
    return out


async def audio_features(conn, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    try:
        rows = await conn.fetch(
            "SELECT source, source_item_id, features FROM item_audio a JOIN unnest($1::text[], $2::text[]) AS k(s, i) "
            "ON a.source = k.s AND a.source_item_id = k.i WHERE a.status = 'done'",
            [k.split(":", 1)[0] for k in keys], [k.split(":", 1)[1] for k in keys])
    except Exception:
        return {}
    return {f"{r['source']}:{r['source_item_id']}": _j(r["features"]) for r in rows}


def pref_prob(model: dict | None, feats: dict | None) -> float | None:
    if not model or not feats or "energy" not in feats:
        return None
    from muse_schema.audio import feature_vector

    x = feature_vector(feats)
    z = sum(((xi - m) / s) * w for xi, m, s, w in zip(x, model["mean"], model["std"], model["weights"])) + model["bias"]
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


async def featurize(conn, qd, p: Person, ctx: Ctx, found: dict[str, set[str]], total_sources: int = 7,
                    cooccur_scores: dict[str, float] | None = None) -> list[Item]:
    keys = list(found)
    pids = {k: content_point_id(*k.split(":", 1)) for k in keys}
    text = _retrieve_many(qd, "content_items", list(pids.values()))
    audio = _retrieve_many(qd, "audio_items", list(pids.values())) if p.item_type == "track" else {}
    beh = _retrieve_many(qd, "behavior_items", list(pids.values()))
    feats_by_key = await audio_features(conn, keys) if p.item_type == "track" else {}
    if cooccur_scores is None:
        cooccur_scores = {}
        recent = p.recent_positive[:15]
        if recent:
            try:
                for r in await conn.fetch(
                    "SELECT to_key, sum(count + 2 * completions - 2 * skips) AS s FROM item_transition "
                    "WHERE from_key = ANY($1::text[]) AND to_key = ANY($2::text[]) GROUP BY 1", recent, keys):
                    cooccur_scores[r["to_key"]] = float(r["s"])
            except Exception:
                pass
    top_co = max(cooccur_scores.values(), default=0) or 1.0
    text_taste = blend([(p.taste, 1.0), (p.context_taste, 0.25)]) if p.taste else None

    items: list[Item] = []
    for k in keys:
        pid = pids[k]
        tp = text.get(pid)
        if tp is None:
            continue  # not embedded: nothing to judge it on
        payload = tp.payload or {}
        if payload.get("item_type") != p.item_type:
            continue
        tv = list(tp.vector)
        ap = audio.get(pid)
        av = list(ap.vector) if ap is not None else None
        bp = beh.get(pid)
        genres = payload.get("genres", []) or []
        gf = genre_features(genres, p.genre_stats, total_sources)
        f: dict[str, float] = {
            "similarity": cos(text_taste, tv) or 0.0,
            "recency_of_similar": math.exp(-gf["recency_of_similar_days"] / 30.0),
            "within_user_popularity": gf["within_user_popularity"],
            "completion_rate": gf["completion_rate"],
            "confidence": gf["confidence"],
        }
        best_facet, best = None, -1.0
        for fc in p.facets:
            s = cos(fc.vector, tv if fc.space == "text" else av)
            if s is not None and s > best:
                best, best_facet = s, fc
        if best_facet is not None:
            f["facet_fit"] = best
        if av is not None and p.audio_taste is not None:
            f["audio_sim"] = cos(p.audio_taste, av) or 0.0
        if bp is not None and p.behavior_taste is not None:
            f["behavior_sim"] = cos(p.behavior_taste, list(bp.vector)) or 0.0
        if k in cooccur_scores:
            f["cooccur"] = max(0.0, cooccur_scores[k]) / top_co
        af = feats_by_key.get(k) or {}
        prob = pref_prob(p.pref_model, af)
        if prob is not None:
            f["preference"] = (prob - 0.5) * 2
        if p.context_taste is not None:
            f["context_fit"] = cos(p.context_taste, tv) or 0.0
        if ctx.mood:
            parts = [x for x in (cos(ctx.mood_text_vec, tv), cos(ctx.mood_audio_vec, av), M.audio_fit(af, ctx.mood_def)) if x is not None]
            if parts:
                f["mood_fit"] = sum(parts) / len(parts)
        if "fresh" in found[k]:
            f["freshness"] = 1.0
        goal = None
        for g in p.goals:
            gs = cos((g["criteria"] or {}).get("vec"), tv)
            gg = {x.lower() for x in (g["criteria"] or {}).get("genres", [])}
            hit = max(gs or 0.0, 1.0 if gg & {x.lower() for x in genres} else 0.0)
            if hit > f.get("goal_fit", 0.0) and hit > 0.45:
                f["goal_fit"], goal = hit, g["title"]
        conf = f["confidence"]
        if p.taste is None and av is None:
            conf = min(conf, 0.3)
        it = Item(source=k.split(":", 1)[0], source_item_id=k.split(":", 1)[1], item_type=p.item_type,
                  similarity=f["similarity"], recency_of_similar_days=gf["recency_of_similar_days"],
                  within_user_popularity=gf["within_user_popularity"], completion_rate=gf["completion_rate"],
                  confidence=conf, genres=genres, title=payload.get("title", ""))
        it.generators, it.features, it.audio, it.payload, it.goal = found[k], f, af, payload, goal
        it.tv, it.av, it.bv = tv, av, (list(bp.vector) if bp is not None else None)
        it.artist = (payload.get("title", "").split(" - ")[0] if p.item_type == "track" else "")
        if best_facet is not None:
            it.facet, it.facet_key = best_facet.label, f"{best_facet.space}:{best_facet.idx}"
        items.append(it)
    return items


# --- scoring & re-ranking --------------------------------------------------------------------------------


def score(items: list[Item], weights: dict[str, float] | None = None) -> list[Item]:
    w = weights or WEIGHTS
    for it in items:
        it.reasons = {k: w[k] * v for k, v in it.features.items() if k in w and v}
        it.score = sum(it.reasons.values())
    return items


def penalize_ignored(items: list[Item], ignored: dict[str, int]) -> None:
    for it in items:
        n = ignored.get(it.key, 0)
        if n >= 3:
            pen = -0.06 * (n - 2)
            it.reasons["shown_but_ignored"] = pen
            it.score += pen


def rerank(items: list[Item], limit: int, explore_ratio: float, facets: list[Facet], *, max_per_genre: int = 3,
           max_per_artist: int = 2) -> list[Item]:
    ranked = sorted(items, key=lambda c: -c.score)
    explore_slots = max(1, round(limit * explore_ratio)) if ranked and explore_ratio > 0 else 0
    exploit_slots = limit - explore_slots
    selected: list[Item] = []
    genre_n: dict[str, int] = defaultdict(int)
    artist_n: dict[str, int] = defaultdict(int)

    def fits(c: Item) -> bool:
        if c.artist and artist_n[c.artist.casefold()] >= max_per_artist:
            return False
        return all(genre_n[g] < max_per_genre for g in c.genres)

    def take(c: Item) -> None:
        selected.append(c)
        for g in c.genres:
            genre_n[g] += 1
        if c.artist:
            artist_n[c.artist.casefold()] += 1

    # 1. Each facet gets room in proportion to its share (at least one slot if it has candidates).
    for fc in sorted(facets, key=lambda f: -f.share):
        quota = max(1, int(exploit_slots * fc.share * 0.6))
        key = f"{fc.space}:{fc.idx}"
        for c in ranked:
            if quota <= 0 or len(selected) >= exploit_slots:
                break
            if c.facet_key == key and c.confidence >= 0.5 and c not in selected and fits(c):
                take(c)
                quota -= 1
    # 2. Best remaining confident items.
    for c in ranked:
        if len(selected) >= exploit_slots:
            break
        if c not in selected and c.confidence >= 0.5 and fits(c):
            take(c)
    # 3. Explore slots: uncertain but promising.
    n_explore = 0
    for c in ranked:
        if n_explore >= explore_slots:
            break
        if c not in selected and c.confidence < 0.5 and fits(c):
            take(c)
            n_explore += 1
    # 4. Backfill, relaxing the caps.
    for c in ranked:
        if len(selected) >= limit:
            break
        if c not in selected:
            take(c)
    return selected[:limit]


def describe(c: Item, facts: list[dict] | None, ctx: Ctx | None, pref_summary: list[str] | None = None) -> list[str]:
    from .scoring import _mentions

    f, why = c.features, []
    if f.get("audio_sim", 0) >= 0.5:
        why.append((f["audio_sim"] + 0.1, "Sounds like the music you love"))
    if c.facet and f.get("facet_fit", 0) >= 0.45:
        why.append((f["facet_fit"], f"Fits your {c.facet} side"))
    if f.get("similarity", 0) >= 0.4:
        why.append((f["similarity"], "Close to what you enjoy" if f["similarity"] < 0.55 else "Very close to what you enjoy"))
    if c.reasons.get("stated_preference", 0) > 0:
        subjects = [x["subject"] for x in facts or [] if x.get("subject") and x["kind"] == "likes" and _mentions(x["subject"], c)]
        why.append((2.0, f"You said you like {subjects[0]}" if subjects else "Matches something you told Muse"))
    if f.get("cooccur", 0) >= 0.3:
        why.append((0.5 + f["cooccur"], "Often played after songs you just played" if c.item_type == "track" else "Often watched after what you watched"))
    if f.get("behavior_sim", 0) >= 0.5:
        why.append((f["behavior_sim"], "Played by people with your habits here"))
    if f.get("preference", 0) >= 0.4 and pref_summary:
        why.append((0.6, pref_summary[0].replace("prefers", "Has the", 1) + " you like"))
    if ctx and ctx.mood and f.get("mood_fit", 0) >= 0.55:
        why.append((1.5, f"For your {ctx.mood} mood" if len(ctx.mood) < 25 else "Fits your mood"))
    if f.get("context_fit", 0) >= 0.5 and ctx and ctx.buckets:
        why.append((0.55, f"Fits your {ctx.buckets[0]} listening" if c.item_type == "track" else f"Fits your {ctx.buckets[0]} viewing"))
    if c.goal:
        why.append((1.4, f"For your goal: {c.goal}"))
    if "fresh" in c.generators:
        why.append((0.45, "New in your library"))
    if c.source == "catalog":
        rel = c.payload.get("related_to")
        why.append((0.5, f"Not in your library yet; related to {rel}" if rel else "Not in your library yet"))
    if f.get("completion_rate", 0) >= 0.6 and c.genres:
        why.append((f["completion_rate"] * 0.5, f"You usually finish {c.genres[0]}"))
    if c.confidence < 0.5:
        why.append((-1, "Something new to try"))
    return [t for _, t in sorted(why, key=lambda x: -x[0])][:4]
