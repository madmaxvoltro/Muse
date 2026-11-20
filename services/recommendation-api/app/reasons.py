"""Reason codes for "Why this". Muse decides *why*; clients do the wording and translation.

Every reason is {code, strength, ...} with optional anchor_id / anchor_title (the title that
anchors it) and value (a name, genre, query or mood). Codes:

  because_loved | because_favorited | because_liked | because_finished | because_watched
      — close to a title in the person's history (anchor_*)
  same_director, same_cast  — shares a director / actor with a title they liked (value, anchor_*)
  watched_together          — group rows: close to something the group watched together (anchor_*)
  genre_affinity            — one of their strongest genres (value)
  searched_for              — close to something they searched for recently (value)
  mood                      — fits the active mood (value = mood id)
  highly_rated              — strong community rating (value, 0-10)
  new_arrival               — added to the library recently
  explore_pick              — deliberately outside the usual taste, to learn
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .taste import cos

KIND_WEIGHT = {"loved": 1.15, "favorited": 1.1, "liked": 1.0, "finished": 0.95, "watched": 0.75}
ANCHOR_MIN = 0.42
SEARCH_MIN = 0.45
MOOD_MIN = 0.28
MAX_REASONS = 4


@dataclass
class Anchor:
    key: str  # "jellyfin:<id>" or "series:<series id>"
    source_item_id: str
    title: str
    kind: str
    strength: float
    recency: float  # 0-1
    vector: list
    directors: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    together: set[str] = field(default_factory=set)


def top_genres(anchors: list[Anchor], n: int = 3) -> list[str]:
    score: dict[str, float] = {}
    for a in anchors:
        for g in a.genres:
            score[g] = score.get(g, 0.0) + a.strength * (0.5 + 0.5 * a.recency)
    return [g for g, _ in sorted(score.items(), key=lambda kv: -kv[1])[:n]]


def best_anchor(vector, anchors: list[Anchor], skip_key: str | None = None) -> tuple[Anchor | None, float]:
    best, best_val, best_sim = None, 0.0, 0.0
    for a in anchors:
        if a.key == skip_key or a.source_item_id == skip_key:
            continue
        sim = cos(vector, a.vector)
        val = sim * KIND_WEIGHT.get(a.kind, 0.8) * (0.7 + 0.3 * a.recency)
        if val > best_val:
            best, best_val, best_sim = a, val, sim
    return best, best_sim


def _is_new(added_at: str | None, days: int = 21) -> bool:
    if not added_at:
        return False
    try:
        ts = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).days <= days


def reasons_for(
    vector,
    payload: dict,
    anchors: list[Anchor],
    genres_top: list[str],
    searches: list[tuple[str, list]] = (),
    mood: tuple[str, list | None] | None = None,
    explore: bool = False,
    group_members: set[str] | None = None,
    self_key: str | None = None,
) -> list[dict]:
    out: list[dict] = []

    if group_members:
        shared = [a for a in anchors if a.together & group_members] if group_members else []
        a, sim = best_anchor(vector, shared, self_key)
        if a and sim >= ANCHOR_MIN:
            out.append({"code": "watched_together", "strength": sim + 0.1, "anchor_id": a.source_item_id, "anchor_title": a.title})

    a, sim = best_anchor(vector, anchors, self_key)
    if a and sim >= ANCHOR_MIN:
        out.append({"code": f"because_{a.kind}", "strength": sim, "anchor_id": a.source_item_id, "anchor_title": a.title})

    directors = set(payload.get("directors") or [])
    cast = list(payload.get("cast") or [])
    liked = sorted((x for x in anchors if x.strength > 0 and self_key not in (x.key, x.source_item_id)), key=lambda x: -x.strength * (0.5 + x.recency))
    for x in liked:
        hit = directors.intersection(x.directors)
        if hit:
            out.append({"code": "same_director", "strength": 0.6 + 0.1 * x.strength, "value": sorted(hit)[0], "anchor_id": x.source_item_id, "anchor_title": x.title})
            break
    for x in liked:
        hit = [c for c in cast[:4] if c in x.cast]
        if hit:
            out.append({"code": "same_cast", "strength": 0.5 + 0.1 * x.strength, "value": hit[0], "anchor_id": x.source_item_id, "anchor_title": x.title})
            break

    for g in payload.get("genres") or []:
        if g in genres_top:
            out.append({"code": "genre_affinity", "strength": 0.4 - 0.05 * genres_top.index(g), "value": g})
            break

    for q, qv in searches:
        if qv is not None and cos(vector, qv) >= SEARCH_MIN:
            out.append({"code": "searched_for", "strength": 0.55, "value": q})
            break

    if mood and mood[0]:
        mood_id, mv = mood
        if mv is None or cos(vector, mv) >= MOOD_MIN:
            out.append({"code": "mood", "strength": 0.5, "value": mood_id})

    cr = payload.get("community_rating")
    if isinstance(cr, (int, float)) and cr >= 7.8:
        out.append({"code": "highly_rated", "strength": 0.3, "value": round(float(cr), 1)})
    if _is_new(payload.get("added_at")):
        out.append({"code": "new_arrival", "strength": 0.35})
    if explore:
        out.append({"code": "explore_pick", "strength": 0.45})

    out.sort(key=lambda r: -r["strength"])
    for r in out:
        r["strength"] = round(float(r["strength"]), 3)
    return out[:MAX_REASONS]
