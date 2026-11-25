"""A home screen as rows, each with its own reason as its title.

A page of themed rows works better than one long list: each row's title *is* its explanation
("Because you watched Arrival", "More slow-burn Nordic crime", "Watch again"), people scan rows
rather than lists, and different needs (something safe, something new, something you meant to
watch) get their own place. This is how Netflix builds its homepage (Gomez-Uribe & Hunt, "The
Netflix Recommender System: Algorithms, Business Value, and Innovation", ACM TMIS 2015).

Page-level rules: a title appears at most once on the page (earlier rows win), rows with too
few good titles are dropped, and every item is logged as an impression with its row as the
surface, so learning knows where it was shown.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import numpy as np
from muse_taste.canon import domain_of
from muse_taste.evolution import adjustment

from .engine import TARGET_TYPES, Engine, Request

MIN_ROW = 4


async def build(engine: Engine, user_id: str, item_type: str, per_row: int = 12, max_rows: int = 8,
                at: datetime | None = None, log: bool = True) -> dict:
    res = await engine.recommend(Request(user_id, item_type, limit=per_row, log=False, at=at, surface="rows"))
    prof, titles, pool = res.profile, res.titles, res.pool
    used: set[str] = set()
    rows: list[dict] = []
    logged: list[tuple[str, list]] = []

    def add(row_id: str, title: str, items: list, reason: dict | None = None, describe=None, min_items: int = MIN_ROW,
            seed: dict | None = None) -> None:
        if len(rows) >= max_rows:
            return
        fresh = [s for s in items if (s.key if hasattr(s, "key") else s["canon_key"]) not in used][:per_row]
        if len(fresh) < min_items:
            return
        for s in fresh:
            used.add(s.key if hasattr(s, "key") else s["canon_key"])
        rows.append({"id": row_id, "title": title, "reason": reason, **({"seed": seed} if seed else {}),
                     "items": [describe(s) if describe else engine.describe(s, prof, titles) for s in fresh]})
        if describe is None:
            logged.append((row_id, fresh))

    add("top", "Top picks for you", res.items)
    if prof is None or res.cold_start:
        add("popular", "Well loved in your library", sorted(pool, key=lambda s: -s.features.get("quality", 0)))
        return await _finish(engine, user_id, item_type, rows, logged, log)

    # Because you watched <title>: the strongest recent loves that have close neighbours
    anchors = [a for a in prof.anchors if a.preference > 0.8][:6]
    n_because = 0
    for a in anchors:
        near = sorted((s for s in pool if s.nearest_anchor == a.key and s.features.get("item_sim", 0) > 0), key=lambda s: -s.features["item_sim"])
        title = titles.get(a.key) or (await engine.titles_for([a.key])).get(a.key)
        if title and len(near) >= MIN_ROW:
            add(f"because:{a.key}", f"Because you watched {title}", near,
                {"id": f"anchor:{a.key}", "type": "anchor", "responses": ["not_relevant", "more"]},
                seed=await _seed(engine, a.key, title, item_type))
            n_because += 1
        if n_because >= 2:
            break

    # When you usually watch: the interest whose rhythm fits right now
    adj = adjustment(prof, at=at)
    if adj is not None and adj.context.size and float(adj.context.max()) > 0.25:
        k = int(np.argmax(adj.context))
        add(f"now:{k}", f"For your {adj.context_label}s: {_name(prof, k, titles)}", _in_interest(pool, k),
            {"id": f"interest:{prof.interests[k].medoid_key}", "type": "interest", "responses": ["more", "less"]})

    # More of your biggest interests
    for it in sorted(prof.interests, key=lambda i: -i.importance)[:3]:
        add(f"interest:{it.id}", f"More {_name(prof, it.id, titles)}", _in_interest(pool, it.id),
            {"id": f"interest:{it.medoid_key}", "type": "interest", "responses": ["more", "less"]})

    # Watch again / Listen again (ACT-R re-consumption)
    if prof.reconsume:
        again = await _reconsume_items(engine, prof, item_type)
        verb = "Listen again" if domain_of(TARGET_TYPES.get(item_type, [item_type])[0]) == "music" else "Watch again"
        add("again", verb, again, {"id": "reconsume", "type": "info", "responses": []}, describe=lambda d: d, min_items=1)

    add("watchlist", "From your watchlist", sorted((s for s in pool if s.features.get("intent", 0) > 0), key=lambda s: -s.score))
    add("new", "New in your library", sorted((s for s in pool if s.features.get("freshness", 0) > 0.3), key=lambda s: -s.score))
    # Something different: the frontier of your taste (stretch) and genuinely uncertain picks
    frontier = sorted((s for s in pool if s.features.get("stretch", 0) > 0 or s.sigma > 0.4),
                      key=lambda s: -(s.score + 0.6 * s.features.get("stretch", 0) + s.sigma))
    add("discover", "Something different for you", frontier,
        {"id": "explore", "type": "explore", "responses": []})
    return await _finish(engine, user_id, item_type, rows, logged, log)


async def _seed(engine: Engine, key: str, title: str, item_type: str) -> dict:
    """The title a "Because you watched" row is about, with its library id so a client can show
    its own artwork and link to it."""
    got: dict = {}
    types = sorted({t for ts in TARGET_TYPES.values() for t in ts})  # you can watch a film because of a series
    await asyncio.to_thread(engine._by_keys, [key], types, True, got)
    refs = (got.get(key, (None, {}))[1] or {}).get("library_refs") or []
    seed = {"canon_key": key, "title": title}
    if refs:
        seed.update(source=refs[0]["source"], source_item_id=refs[0]["source_item_id"])
    return seed


def _in_interest(pool: list, k: int) -> list:
    return sorted((s for s in pool if s.interest_id == k), key=lambda s: -s.score)


def _name(prof, k: int, titles: dict) -> str:
    it = prof.interests[k]
    return it.label or ", ".join(t.split(":", 1)[-1] for t in it.top_tags[:2]) or titles.get(it.medoid_key, "this")


async def _reconsume_items(engine: Engine, prof, item_type: str) -> list[dict]:
    types = TARGET_TYPES.get(item_type, [item_type])
    wanted = [r["key"] for r in prof.reconsume]
    why = {r["key"]: r["why"] for r in prof.reconsume}
    pool: dict = {}
    keys = await asyncio.to_thread(engine._by_keys, wanted, types, False, pool)
    out = []
    for k in keys:
        _, pl = pool[k]
        refs = pl.get("library_refs") or []
        if not refs:
            continue
        out.append({
            "source": refs[0]["source"], "source_item_id": refs[0]["source_item_id"], "canon_key": k,
            "title": pl.get("title"), "year": pl.get("year"), "item_type": pl.get("item_type"), "available": True,
            "reasons": [why[k][0].upper() + why[k][1:]],
            "reason_details": [{"text": why[k][0].upper() + why[k][1:], "type": "reconsume", "id": "reconsume", "code": "watch_again",
                                "args": {}, "responses": []}],
        })
    return out


async def _finish(engine: Engine, user_id: str, item_type: str, rows: list[dict], logged: list, log: bool) -> dict:
    request_id = str(uuid.uuid4())
    if log:
        for row_id, items in logged:
            await engine.log_impressions(request_id, user_id, f"rows:{row_id.split(':', 1)[0]}", items)
    return {"user_id": user_id, "item_type": item_type, "request_id": request_id, "rows": rows}
