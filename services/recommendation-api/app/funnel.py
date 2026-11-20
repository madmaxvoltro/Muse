"""The recommendation funnel behind every row type.

  1. Profiles   — per person: history (history.py), liked "anchors" with vectors, dislikes,
                  taste facets (taste.py), recent searches, overlay (explore dial, mood).
  2. Candidates — Qdrant searches per facet / seed / mood / group member, seen items excluded
                  in the query itself, episodes rolled up to their series.
  3. Scoring    — facet affinity (group: average + least misery), seed similarity for seeded
                  rows, the stage-2 features from candidates.py, a quality prior, penalties
                  for resembling dislikes and for being shown often without a click.
  4. Re-ranking — MMR for variety, a genre cap, and explore slots spread through the row.
  5. Reasons    — reason codes per pick (reasons.py) and a match percentage.

Row types (Params.row):
  picks               taste facets (+ mood)                         "Top picks for you"
  because_you_watched seeded by a liked title (Muse picks one)      "Because you watched X"
  more_like_this      seeded by the title on screen + explore dial  detail page
  new_arrivals        picks restricted to recently added titles     "New for you"
  rewatch             loved titles not seen for a while             "Comfort rewatch" mood
  group               several people at once                        "For the two of you"
"""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import asyncpg
from muse_schema import content_point_id, taste_point_id
from qdrant_client import QdrantClient
from qdrant_client.http.models import DatetimeRange, FieldCondition, Filter, HasIdCondition, MatchAny

from . import embed, history, moods, overlay, ratings
from .candidates import genre_features, get_user_genre_stats
from .reasons import Anchor, reasons_for, top_genres
from .taste import Facet, affinity, blend, cos, facets, group_affinity, match_percent, max_similarity, mmr, unit

CONTENT = "content_items"
TASTE = "taste_vectors"
POOL_PER_QUERY = 120
EXCLUDE_IDS_MAX = 3000
DIVERSITY_MAX_PER_GENRE = 4
DISLIKE_DROP = 0.72  # this close to something they gave a thumbs down: leave it out entirely
SEED_MIN = 0.25
MIN_AFFINITY = 0.15  # below a coin flip ("50 % match"): not worth a slot in a taste row  # a "Because you watched X" pick must actually resemble X

ROWS = ("picks", "because_you_watched", "more_like_this", "new_arrivals", "rewatch", "group")


@dataclass
class Params:
    user_ids: list[str]
    item_types: list[str]
    row: str = "picks"
    seed: str | None = None
    seed_source: str = "jellyfin"
    seed_rank: int = 0
    limit: int = 20
    explore_ratio: float | None = None
    max_age: int | None = None
    allow_unrated: bool = False
    exclude: set[str] = field(default_factory=set)
    mood: str | None = None  # None: use the overlay's; "": no mood
    include_seen: bool = False


@dataclass
class Profile:
    user_id: str
    items: dict[str, history.Item]
    anchors: list[Anchor]
    dislikes: list[list]
    facets: list[Facet]
    taste: list | None
    searches: list[tuple[str, list | None]]
    genres_top: list[str]
    seen_keys: set[str]
    seen_point_ids: set[str]
    overlay: dict

    @property
    def cold(self) -> bool:
        return self.taste is None and not self.facets


@dataclass
class Scored:
    source: str
    source_item_id: str
    item_type: str
    title: str
    payload: dict
    vector: list
    query_sim: float = 0.0
    affinity: float = 0.0
    member_affinity: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    score: float = 0.0
    explanation: dict[str, float] = field(default_factory=dict)
    explore: bool = False
    reasons: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_item_id}"


# --- profiles ---------------------------------------------------------------------------


def _retrieve(qdrant: QdrantClient, ids: list[str], vectors: bool = True) -> dict[str, object]:
    out = {}
    for i in range(0, len(ids), 256):
        for p in qdrant.retrieve(collection_name=CONTENT, ids=ids[i : i + 256], with_payload=True, with_vectors=vectors):
            out[str(p.id)] = p
    return out


def _taste_vector(qdrant: QdrantClient, user_id: str, item_types: list[str]) -> list | None:
    types = [("series" if t == "series_episode" else t) for t in item_types]
    ids = [taste_point_id(user_id, t) for t in dict.fromkeys(types + ["series_episode"] if "series" in types else types)]
    pts = qdrant.retrieve(collection_name=TASTE, ids=ids, with_vectors=True)
    if not pts:
        pts = qdrant.retrieve(collection_name=TASTE, ids=[taste_point_id(user_id, "_combined")], with_vectors=True)
    vecs = [p.vector for p in pts if p.vector]
    return list(blend([(v, 1.0) for v in vecs])) if vecs else None


def _video_like(item_type: str, wanted: list[str]) -> bool:
    if item_type in wanted:
        return True
    return item_type == "series_episode" and "series" in wanted


async def build_profile(pool: asyncpg.Pool, qdrant: QdrantClient, user_id: str, item_types: list[str]) -> Profile:
    items, search_texts = await history.load(pool, user_id)
    ov = await overlay.get(pool, user_id)

    point_of = {k: content_point_id(it.source, it.source_item_id) for k, it in items.items()}
    relevant = [k for k, it in items.items() if it.strength != 0 and (it.strength < 0 or _video_like(it.item_type, item_types) or it.item_type in item_types)]
    pts = _retrieve(qdrant, [point_of[k] for k in relevant])

    grouped: dict[str, dict] = {}
    dislikes: list[list] = []
    seen_keys: set[str] = set()
    seen_point_ids: set[str] = set()
    for k, it in items.items():
        if it.seen:
            seen_keys.add(k)
            seen_point_ids.add(point_of[k])
    for k in relevant:
        it, p = items[k], pts.get(point_of[k])
        if p is None or p.vector is None:
            continue
        pl = p.payload or {}
        if it.strength < 0:
            dislikes.append(p.vector)
            continue
        # Episodes speak for their series: one anchor per show, not forty.
        series_id = pl.get("series_id") if it.item_type == "series_episode" else None
        if series_id:
            seen_keys.add(f"{it.source}:{series_id}")
            seen_point_ids.add(content_point_id(it.source, series_id))
        akey = f"series:{series_id}" if series_id else k
        g = grouped.setdefault(
            akey,
            {"items": [], "vectors": [], "title": pl.get("series_name") if series_id else pl.get("title", ""),
             "sid": series_id or it.source_item_id, "pl": pl},
        )
        g["items"].append(it)
        g["vectors"].append(p.vector)

    anchors: list[Anchor] = []
    for akey, g in grouped.items():
        its: list[history.Item] = g["items"]
        best = max(its, key=lambda x: x.strength)
        strength = best.strength + (0.15 * min(len(its) - 1, 6) if akey.startswith("series:") else 0.0)  # many episodes = a show they're into
        last = max((x.last_ts for x in its if x.last_ts), default=None)
        pl = g["pl"]
        anchors.append(
            Anchor(
                key=akey, source_item_id=g["sid"], title=g["title"] or "", kind=best.kind, strength=strength,
                recency=history.recency_weight(last), vector=list(blend([(v, 1.0) for v in g["vectors"][-8:]])),
                directors=list(pl.get("directors") or []), cast=list(pl.get("cast") or []), genres=list(pl.get("genres") or []),
                together=set().union(*(x.together for x in its)),
            )
        )

    weights = [a.strength * (0.35 + 0.65 * a.recency) for a in anchors]
    fs = facets([a.vector for a in anchors], weights) if anchors else []
    taste = _taste_vector(qdrant, user_id, item_types)
    vecs = await embed.embed(search_texts) if search_texts else []
    return Profile(
        user_id=user_id, items=items, anchors=anchors, dislikes=dislikes, facets=fs, taste=taste,
        searches=list(zip(search_texts, vecs)), genres_top=top_genres(anchors), seen_keys=seen_keys,
        seen_point_ids=seen_point_ids, overlay=ov,
    )


# --- candidates -------------------------------------------------------------------------


def _filter(types: list[str], exclude_ids: set[str], added_since: datetime | None) -> Filter:
    search_types = list(dict.fromkeys(types + (["series_episode"] if "series" in types else [])))
    must = [FieldCondition(key="item_type", match=MatchAny(any=search_types))]
    if added_since is not None:
        must.append(FieldCondition(key="added_at", range=DatetimeRange(gte=added_since)))
    must_not = [HasIdCondition(has_id=sorted(exclude_ids)[:EXCLUDE_IDS_MAX])] if exclude_ids else []
    return Filter(must=must, must_not=must_not)


def _search(qdrant: QdrantClient, vector, flt: Filter, limit: int):
    return qdrant.query_points(
        collection_name=CONTENT, query=list(unit(vector)), query_filter=flt, limit=limit, with_payload=True, with_vectors=True
    ).points


def _rolled(payload: dict, types: list[str]) -> tuple[str, str, str, str]:
    """(source, source_item_id, item_type, title) the hit is shown as; episodes become their series."""
    src = payload.get("source", "")
    if payload.get("item_type") == "series_episode" and "series" in types and payload.get("series_id"):
        return src, payload["series_id"], "series", payload.get("series_name") or payload.get("title", "")
    return src, payload.get("source_item_id", ""), payload.get("item_type", ""), payload.get("title", "")


def _allowed(payload: dict, p: Params, mood: moods.Mood | None) -> bool:
    if not ratings.within(payload.get("official_rating"), p.max_age, p.allow_unrated):
        return False
    if mood and mood.max_runtime_minutes:
        rt = payload.get("runtime_minutes")
        if payload.get("item_type") == "movie" and (rt is None or rt > mood.max_runtime_minutes):
            return False
    return True


# --- the funnel -------------------------------------------------------------------------


async def recommend(pool: asyncpg.Pool, qdrant: QdrantClient, p: Params) -> dict:
    if p.row not in ROWS:
        raise ValueError(f"row must be one of {ROWS}")
    profiles = await asyncio.gather(*(build_profile(pool, qdrant, u, p.item_types) for u in p.user_ids))
    primary = profiles[0]
    group = len(profiles) > 1
    members = set(p.user_ids)

    mood_id = primary.overlay.get("mood") if p.mood is None else (p.mood or None)
    mood = moods.get(mood_id) if p.row in ("picks", "group", "new_arrivals") else None
    mood_vec = await embed.embed_one(mood.prompt) if mood and mood.prompt else None
    explore_ratio = p.explore_ratio if p.explore_ratio is not None else primary.overlay.get("explore_ratio", 0.125)

    if mood and mood.rewatch and p.row == "picks":
        p.row = "rewatch"
    if p.row == "rewatch":
        return _rewatch(qdrant, primary, p, mood_id)
    if all(pr.cold for pr in profiles) and p.row not in ("more_like_this",):
        return _response(p, [], note="cold-start: no taste yet", mood=mood_id)

    # Query vectors for this row.
    seed_info = None
    queries: list[tuple[list, float]] = []  # (vector, weight of seed similarity in the score)
    seed_vec = None
    if p.row in ("because_you_watched", "more_like_this"):
        seed_info, seed_vec = _seed(qdrant, primary, p)
        if seed_vec is None:
            return _response(p, [], note="no seed", mood=mood_id)
        # Familiar leans on the profile's taste, Explore on the seed alone.
        taste_w = max(0.05, min(0.4, 0.4 - 0.85 * explore_ratio)) if p.row == "more_like_this" else 0.2
        taste_mix = primary.taste if primary.taste is not None else (primary.facets[0].centroid if primary.facets else None)
        queries.append((list(blend([(seed_vec, 1 - taste_w), (taste_mix, taste_w)])), 0.65))
    else:
        for pr in profiles:
            for f in pr.facets:
                queries.append((list(blend([(f.centroid, 1 - (mood.weight if mood_vec else 0)), (mood_vec, mood.weight if mood_vec else 0)])), 0.0))
            if pr.taste is not None:
                queries.append((list(blend([(pr.taste, 1 - (mood.weight if mood_vec else 0)), (mood_vec, mood.weight if mood_vec else 0)])), 0.0))
        if group:
            tastes = [pr.taste for pr in profiles if pr.taste is not None]
            if tastes:
                queries.append((list(blend([(t, 1.0) for t in tastes])), 0.0))

    exclude_ids: set[str] = set()
    if not p.include_seen:
        for pr in profiles:
            exclude_ids |= pr.seen_point_ids if not group else set()
        if group:  # seen by everyone: skip; seen by some: penalised below
            exclude_ids = set.intersection(*(pr.seen_point_ids for pr in profiles)) if profiles else set()
    if p.row in ("because_you_watched", "more_like_this") and p.seed:
        exclude_ids.add(content_point_id(p.seed_source, p.seed))
    added_since = datetime.now(timezone.utc) - timedelta(days=45) if p.row == "new_arrivals" else None
    flt = _filter(p.item_types, exclude_ids, added_since)

    hits = await asyncio.gather(*(asyncio.to_thread(_search, qdrant, q, flt, POOL_PER_QUERY) for q, _ in queries))

    # Merge hits per shown item (episodes rolled up), keep the best query similarity.
    cands: dict[str, Scored] = {}
    for (q, seed_w), points in zip(queries, hits):
        for h in points:
            pl = h.payload or {}
            if h.vector is None or not _allowed(pl, p, mood):
                continue
            src, sid, typ, title = _rolled(pl, p.item_types)
            key = f"{src}:{sid}"
            if sid in p.exclude or key in p.exclude or (p.seed and sid == p.seed):
                continue
            if not p.include_seen and not group and key in primary.seen_keys:
                continue
            if group and all(key in pr.seen_keys for pr in profiles):
                continue
            c = cands.get(key)
            if c is None or h.score > c.query_sim:
                cands[key] = Scored(src, sid, typ, title, pl if typ == pl.get("item_type") else {**pl, "item_type": typ}, h.vector, query_sim=float(h.score))
            if seed_w:
                cands[key].explanation["_seed_w"] = seed_w

    if not cands and p.row == "more_like_this":
        return _response(p, [], note="no similar titles", seed=seed_info, mood=mood_id)

    genre_stats = await get_user_genre_stats(pool, qdrant, primary.user_id)
    scored = [_score(c, profiles, p, group, mood_vec, genre_stats) for c in cands.values()]
    scored = [c for c in scored if c.explanation.get("_dislike", 0.0) < DISLIKE_DROP]
    if p.row in ("picks", "group", "new_arrivals"):
        scored = [c for c in scored if c.affinity >= MIN_AFFINITY or not c.member_affinity]
    if seed_vec is not None:
        scored = [c for c in scored if cos(c.vector, seed_vec) >= SEED_MIN]
    for c in scored:
        c.explanation.pop("_dislike", None)
    final = _rerank(scored, p.limit, explore_ratio)

    for c in final:
        c.reasons = reasons_for(
            c.vector, c.payload, primary.anchors if not group else [a for pr in profiles for a in pr.anchors],
            primary.genres_top, primary.searches, (mood_id, mood_vec) if mood else None, c.explore,
            members - {primary.user_id} if group else None, c.source_item_id,
        )
        if seed_info and p.row == "because_you_watched" and cos(c.vector, seed_vec) >= 0.35 and not any(r.get("anchor_id") == seed_info["source_item_id"] for r in c.reasons):
            c.reasons.insert(0, {"code": f"because_{seed_info['kind']}", "strength": round(c.query_sim, 3), "anchor_id": seed_info["source_item_id"], "anchor_title": seed_info["title"]})
            c.reasons = c.reasons[:4]
    return _response(p, final, seed=seed_info, mood=mood_id, explore_ratio=explore_ratio, group=group)


def _seed(qdrant: QdrantClient, pr: Profile, p: Params) -> tuple[dict | None, list | None]:
    if p.row == "more_like_this" or p.seed:
        if not p.seed:
            return None, None
        pts = _retrieve(qdrant, [content_point_id(p.seed_source, p.seed)])
        pt = next(iter(pts.values()), None)
        if pt is None or pt.vector is None:
            return None, None
        pl = pt.payload or {}
        it = pr.items.get(f"{p.seed_source}:{p.seed}")
        return {"source_item_id": p.seed, "title": pl.get("title", ""), "kind": it.kind if it else "watched"}, pt.vector
    # Muse picks the anchor: liked, recent, of a requested type; seed_rank picks the n-th.
    eligible = [a for a in pr.anchors if a.strength >= 0.9]
    eligible.sort(key=lambda a: -(a.recency * (0.6 + 0.4 * min(a.strength, 2.0) / 2.0)))
    if p.seed_rank >= len(eligible):
        return None, None
    a = eligible[p.seed_rank]
    p.exclude.add(a.source_item_id)
    return {"source_item_id": a.source_item_id, "title": a.title, "kind": a.kind}, a.vector


def _score(c: Scored, profiles: list[Profile], p: Params, group: bool, mood_vec, genre_stats) -> Scored:
    primary = profiles[0]
    for pr in profiles:
        if pr.cold:
            continue
        c.member_affinity[pr.user_id] = affinity(c.vector, pr.facets, pr.taste)
    affs = list(c.member_affinity.values())
    c.affinity = group_affinity(affs) if group else (affs[0] if affs else c.query_sim)

    seed_w = c.explanation.pop("_seed_w", 0.0)
    sim = seed_w * c.query_sim + (1 - seed_w) * c.affinity

    feats = genre_features(c.payload.get("genres") or [], genre_stats, total_sources=3)
    c.confidence = feats["confidence"]
    e = {
        "similarity": 0.5 * sim,
        "recency_of_similar": 0.12 * math.exp(-feats["recency_of_similar_days"] / 30.0),
        "within_user_popularity": 0.1 * feats["within_user_popularity"],
        "completion_rate": 0.1 * feats["completion_rate"],
        "confidence": 0.08 * feats["confidence"],
    }
    cr = c.payload.get("community_rating")
    if isinstance(cr, (int, float)):
        e["quality"] = 0.08 * (min(max(cr, 0), 10) / 10.0 - 0.6)
    dis = max((max_similarity(c.vector, pr.dislikes) for pr in profiles), default=0.0)
    if dis > 0.45:
        e["dislike_penalty"] = -0.35 * (dis - 0.45)
    it = primary.items.get(c.key)
    if it and it.impressions >= 3 and it.recent_clicks == 0:
        e["fatigue"] = -0.025 * min(it.impressions, 12)  # shown again and again, never picked
    if it and (it.watchlisted or it.requested) and not it.seen:
        e["on_watchlist"] = 0.05
    if mood_vec is not None:
        e["mood"] = 0.12 * cos(c.vector, mood_vec)
    if group:
        seen_by = sum(1 for pr in profiles if c.key in pr.seen_keys)
        if seen_by:
            e["seen_by_some"] = -0.12 * seen_by  # someone in the room has seen it
        shared = [a for pr in profiles for a in pr.anchors if a.together & set(c.member_affinity)]
        if shared:
            e["watched_together"] = 0.06 * max(0.0, max(cos(c.vector, a.vector) for a in shared))
    c.score = sum(e.values())
    e["_dislike"] = dis
    c.explanation = e
    return c


def _rerank(cands: list[Scored], limit: int, explore_ratio: float) -> list[Scored]:
    """Exploit slots by MMR over the best-scoring confident candidates, explore slots from
    lower-confidence but decent ones, interleaved so explore picks aren't all at the end."""
    if not cands:
        return []
    ranked = sorted(cands, key=lambda c: c.score, reverse=True)
    explore_n = round(limit * explore_ratio) if explore_ratio > 0 else 0
    explore_n = min(explore_n, max(0, len(ranked) - 1))
    exploit_n = limit - explore_n

    head = ranked[: max(limit * 4, 40)]
    order = mmr(list(range(len(head))), [c.vector for c in head], [c.score for c in head], len(head))
    genre_count: dict[str, int] = {}

    def fits(c: Scored) -> bool:
        return all(genre_count.get(g, 0) < DIVERSITY_MAX_PER_GENRE for g in (c.payload.get("genres") or [])[:2])

    def take(c: Scored) -> None:
        for g in (c.payload.get("genres") or [])[:2]:
            genre_count[g] = genre_count.get(g, 0) + 1

    exploit: list[Scored] = []
    for i in order:
        c = head[i]
        if len(exploit) >= exploit_n:
            break
        if fits(c):
            exploit.append(c)
            take(c)
    chosen = {id(c) for c in exploit}
    rest = [c for c in ranked if id(c) not in chosen]
    if not explore_n:
        exploit.extend(rest[: limit - len(exploit)])
        return exploit[:limit]

    # Explore: less certain, less similar to the taste, still a reasonable score.
    median = ranked[len(ranked) // 2].score
    pool = sorted([c for c in rest if c.score >= median * 0.8], key=lambda c: (c.confidence, -c.score))
    explore = pool[:explore_n]
    for c in explore:
        c.explore = True
    out = list(exploit)
    step = max(2, len(out) // max(1, len(explore)) if explore else 1)
    for n, c in enumerate(explore):
        out.insert(min(len(out), 3 + n * step), c)
    if len(out) < limit:
        used = {id(c) for c in out}
        out.extend([c for c in rest if id(c) not in used][: limit - len(out)])
    return out[:limit]


def _rewatch(qdrant: QdrantClient, pr: Profile, p: Params, mood_id: str | None) -> dict:
    """Comfort rewatch: loved or finished titles not seen for 60+ days, most loved first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    picks = []
    for k, it in pr.items.items():
        if it.strength < 1.0 or not _video_like(it.item_type, p.item_types) or it.item_type == "series_episode":
            continue
        if it.last_ts and it.last_ts > cutoff:
            continue
        picks.append(it)
    picks.sort(key=lambda it: -(it.strength + 0.1 * it.completions))
    pts = _retrieve(qdrant, [content_point_id(it.source, it.source_item_id) for it in picks[: p.limit * 2]])
    out: list[Scored] = []
    for it in picks[: p.limit * 2]:
        pt = pts.get(content_point_id(it.source, it.source_item_id))
        pl = (pt.payload if pt else None) or {}
        if not ratings.within(pl.get("official_rating"), p.max_age, p.allow_unrated):
            continue
        c = Scored(it.source, it.source_item_id, it.item_type, pl.get("title", ""), pl, pt.vector if pt else [], score=it.strength / 3)
        c.affinity = 0.6
        c.reasons = [{"code": f"because_{it.kind}", "strength": 1.0, "anchor_id": it.source_item_id, "anchor_title": pl.get("title", "")}, {"code": "mood", "strength": 0.5, "value": "comfort"}]
        out.append(c)
        if len(out) >= p.limit:
            break
    return _response(p, out, mood=mood_id or "comfort")


def _response(p: Params, final: list[Scored], note: str | None = None, seed: dict | None = None, mood: str | None = None,
              explore_ratio: float | None = None, group: bool = False) -> dict:
    out = {
        "user_id": p.user_ids[0],
        "user_ids": p.user_ids,
        "item_type": ",".join(p.item_types),
        "row": p.row,
        "seed": seed,
        "mood": mood,
        "explore_ratio": explore_ratio,
        "recommendations": [
            {
                "source": c.source,
                "source_item_id": c.source_item_id,
                "item_type": c.item_type,
                "title": c.title,
                "score": round(c.score, 4),
                "confidence": round(c.confidence, 4),
                "match": match_percent(c.affinity) if c.affinity else None,
                "member_match": {u: match_percent(a) for u, a in c.member_affinity.items()} if group else None,
                "explore": c.explore,
                "reason_codes": c.reasons,
                "explanation": {k: round(v, 4) for k, v in c.explanation.items()},
            }
            for c in final
        ],
    }
    if note:
        out["note"] = note
    return out


# --- explain a single item ------------------------------------------------------------


async def explain(pool: asyncpg.Pool, qdrant: QdrantClient, user_ids: list[str], source: str, source_item_id: str) -> dict:
    """"Why this" for any title (the detail page shows it whether or not it was a pick)."""
    pts = _retrieve(qdrant, [content_point_id(source, source_item_id)])
    pt = next(iter(pts.values()), None)
    if pt is None or pt.vector is None:
        return {"source_item_id": source_item_id, "match": None, "reason_codes": [], "note": "not embedded yet"}
    pl = pt.payload or {}
    types = ["series"] if pl.get("item_type") in ("series", "series_episode") else [pl.get("item_type", "movie")]
    profiles = await asyncio.gather(*(build_profile(pool, qdrant, u, types) for u in user_ids))
    primary = profiles[0]
    if primary.cold:
        return {"source_item_id": source_item_id, "match": None, "reason_codes": [], "note": "cold-start: no taste yet"}
    affs = {pr.user_id: affinity(pt.vector, pr.facets, pr.taste) for pr in profiles if not pr.cold}
    aff = group_affinity(list(affs.values())) if len(profiles) > 1 else affs.get(primary.user_id, 0.0)
    dis = max((max_similarity(pt.vector, pr.dislikes) for pr in profiles), default=0.0)
    if dis > 0.45:
        aff -= 0.35 * (dis - 0.45)
    mood_id = primary.overlay.get("mood")
    mood = moods.get(mood_id)
    mood_vec = await embed.embed_one(mood.prompt) if mood and mood.prompt else None
    anchors = [a for pr in profiles for a in pr.anchors]
    reasons = reasons_for(pt.vector, pl, anchors, primary.genres_top, primary.searches,
                          (mood_id, mood_vec) if mood and mood_vec is not None else None, False,
                          set(user_ids[1:]) or None, source_item_id)
    it = primary.items.get(f"{source}:{source_item_id}")
    return {
        "source_item_id": source_item_id,
        "match": match_percent(aff),
        "member_match": {u: match_percent(a) for u, a in affs.items()} if len(profiles) > 1 else None,
        "reason_codes": reasons,
        "seen": bool(it and it.seen),
        "disliked_similar": dis > 0.6,
    }


