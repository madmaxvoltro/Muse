"""Questions Muse wants a person to answer — the "interview" half of the profile.

Muse generates them from what it can't tell from behaviour alone:

* **cold start** — a domain the household uses (there are film events, say) but this person
  has barely touched: "Which films do you love, and why?" Answers become facts, which the
  embedding worker turns into a taste vector before a single play.
* **mixed signals** — a genre or artist with both strong positive and negative events.
* **explore feedback** — an uncertain recommendation (explore slot): "Would you be into X?"
  Answers become `rated` events on the item, exactly like a thumbs in a player app.
* **settings** — how adventurous recommendations should be; whether music taste may steer
  films (cross-domain blend).

Assistants (via the MCP server) and apps (Mono, via adapter auth) fetch open questions, ask
the person in their own words, and post the answer back. Assistants can also queue their own
questions for later. Each option may carry `effect`s that answering applies:

    {"type": "rate", "value": 2}                         -> rated event on context.item
    {"type": "fact", "kind": "likes", "sentiment": 0.6,
     "statement": "...{text}...", "subject": "...", "domain": "track"}
    {"type": "overlay", "explore_ratio": 0.2}
    {"type": "preference", "key": "taste_blend", "value": 0.3}

Free-text questions keep their effects in context.text_effects ({text} = the answer).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from muse_schema import RATED_WEIGHTS, identity_group
from muse_schema.storage import fetch_open, insert_event

from . import recclient
from .identity import mark_dirty
from .items import DOMAIN_LABELS, item_details
from .knowledge import add_fact, update_overlay

log = logging.getLogger("muse.query.questions")

ITEM_QUESTION_TTL = timedelta(days=14)
EXPLORE_LEVELS = {1: 0.05, 2: 0.1, 3: 0.125, 4: 0.2, 5: 0.3}
COLD_START_EVENTS = 5
UNCERTAIN = 0.7  # recommendation confidence below which Muse would rather ask than guess


def question_json(r) -> dict[str, Any]:
    opts = r["options"] or []
    return {
        "id": str(r["id"]),
        "domain": r["domain"],
        "kind": r["kind"],
        "prompt": r["prompt"],
        "options": [{"value": o["value"], "label": o["label"]} for o in opts],
        "reason": r["reason"],
        "item": (r["context"] or {}).get("item"),
        "created_by": r["created_by"],
        "status": r["status"],
        "answer": r["answer"],
        "how_to_answer": (
            "answer with text" if r["kind"] == "free_text"
            else "answer with one of the option values" + (" (or text, if none fits)" if r["kind"] != "scale" else "")
        ),
    }


async def _insert(conn, user_id: uuid.UUID, q: dict[str, Any]) -> bool:
    row = await conn.fetchrow(
        """
        INSERT INTO question (user_id, domain, kind, prompt, options, context, reason, priority, dedupe_key, created_by, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (user_id, dedupe_key) DO NOTHING RETURNING id
        """,
        user_id, q["domain"], q["kind"], q["prompt"], q.get("options", []), q.get("context", {}), q.get("reason"),
        q.get("priority", 0.5), q.get("dedupe_key"), q.get("created_by", "muse"), q.get("expires_at"),
    )
    return row is not None


# --- generators ---------------------------------------------------------------------------


async def _domain_activity(conn, group: list[uuid.UUID]) -> tuple[dict[str, int], set[str]]:
    mine = {
        r["item_type"]: r["n"]
        for r in await conn.fetch(
            "SELECT item_type, count(*) AS n FROM event WHERE user_id = ANY($1::uuid[]) "
            "AND action NOT IN ('cataloged', 'searched') GROUP BY 1",
            group,
        )
    }
    household = {r["item_type"] for r in await conn.fetch("SELECT DISTINCT item_type FROM event WHERE item_type <> 'search_query' AND item_type <> 'chat'")}
    return mine, household


async def _cold_start(conn, user_id, group) -> list[dict]:
    mine, household = await _domain_activity(conn, group)
    facts = {r["domain"] for r in await conn.fetch(
        "SELECT DISTINCT domain FROM profile_fact WHERE user_id = $1 AND retracted_at IS NULL", user_id)}
    out = []
    for domain in sorted(household):
        if domain == "series_episode":
            continue  # asked as "series"
        if mine.get(domain, 0) >= COLD_START_EVENTS or domain in facts:
            continue
        label = DOMAIN_LABELS.get(domain, domain)
        out.append({
            "domain": domain, "kind": "free_text", "priority": 0.9,
            "prompt": f"Which {label} do you love, and what is it about them?",
            "reason": f"Muse has almost no {label} history for you yet, so it can't recommend any well.",
            "dedupe_key": f"cold:{domain}:likes",
            "context": {"text_effects": [{"type": "fact", "kind": "likes", "sentiment": 0.8, "domain": domain,
                                          "statement": "Loves these " + label + ": {text}"}]},
        })
        out.append({
            "domain": domain, "kind": "free_text", "priority": 0.7,
            "prompt": f"Is there anything in {label} you really don't want suggested?",
            "reason": f"Knowing what to avoid makes early {label} picks much better.",
            "dedupe_key": f"cold:{domain}:dislikes",
            "context": {"text_effects": [{"type": "fact", "kind": "dislikes", "sentiment": -0.7, "domain": domain,
                                          "statement": "Does not want these " + label + ": {text}"}]},
        })
    return out


async def _mixed_signals(conn, user_id, group) -> list[dict]:
    """Genres (and, for music, artists) with substantial positive *and* negative weight."""
    # genres and artists are in the sealed metadata (muse_schema.storage): aggregate in Python
    acc: dict[tuple[str, str, str], list[float]] = {}
    for e in await fetch_open(
        conn,
        """
        SELECT item_type, source, source_item_id, action_weight AS w, metadata FROM event
        WHERE user_id = ANY($1::uuid[]) AND action NOT IN ('cataloged', 'searched') AND action_weight <> 0
          AND "timestamp" > now() - interval '365 days'
        """,
        group,
    ):
        md, w = e["metadata"] or {}, float(e["w"])
        keys = [(e["item_type"], "genre", g) for g in (md.get("genres") if isinstance(md.get("genres"), list) else [])]
        if e["item_type"] == "track" and md.get("artist"):
            keys.append((e["item_type"], "artist", md["artist"]))
        for k in keys:
            a = acc.setdefault(k, [0.0, 0.0, 0])
            a[0 if w > 0 else 1] += abs(w)
            a[2] += 1
    rows = sorted(
        ({"item_type": t, "facet": f, "value": v, "pos": a[0], "neg": a[1], "n": a[2]}
         for (t, f, v), a in acc.items() if a[2] >= 4 and a[0] >= 1.5 and a[1] >= 1.0),
        key=lambda r: -min(r["pos"], r["neg"]),
    )[:4]
    out = []
    for r in rows:
        label = DOMAIN_LABELS.get(r["item_type"], r["item_type"])
        v, domain = r["value"], r["item_type"]
        noun = v if r["facet"] == "artist" else f"{v} {label}"
        out.append({
            "domain": domain, "kind": "choice", "priority": 0.6,
            "prompt": f"You both finish and skip a lot of {noun}. What's closer to the truth?",
            "reason": f"Mixed signals on {v}: {r['n']} events, roughly as many positive as negative.",
            "dedupe_key": f"mixed:{domain}:{r['facet']}:{v.casefold()}",
            "options": [
                {"value": "love", "label": f"I love {v}, I just skip the weaker ones",
                 "effect": [{"type": "fact", "kind": "likes", "sentiment": 0.6, "subject": v, "domain": domain,
                             "statement": f"Likes {noun}, but is picky about it"}]},
                {"value": "mood", "label": "Only in certain moods",
                 "effect": [{"type": "fact", "kind": "context", "sentiment": 0.0, "subject": v, "domain": domain,
                             "statement": f"Enjoys {noun} only in certain moods"}]},
                {"value": "meh", "label": "Not really my thing",
                 "effect": [{"type": "fact", "kind": "dislikes", "sentiment": -0.5, "subject": v, "domain": domain,
                             "statement": f"Is not really into {noun}"}]},
                {"value": "never", "label": "Please stop suggesting it",
                 "effect": [{"type": "fact", "kind": "avoid", "sentiment": -1.0, "subject": v, "domain": domain,
                             "statement": f"Never suggest {noun}"}]},
            ],
        })
    return out


async def _explore_feedback(conn, user_id, group) -> list[dict]:
    mine, _ = await _domain_activity(conn, group)
    if not mine:
        return []
    domain = max(mine, key=mine.get)
    try:
        recs = await recclient.recommendations(str(user_id), domain, limit=12, explore_ratio=0.5)
    except recclient.RecUnavailable as e:
        log.info("no explore questions: %s", e)
        return []
    # The least certain of the decent candidates: explore slots first, then anything below 0.7.
    uncertain = [r for r in recs.get("recommendations", []) if r.get("explore") or r.get("confidence", 1) < UNCERTAIN]
    picks = sorted(uncertain, key=lambda r: r.get("confidence", 1))[:3]
    if not picks:
        return []
    details = await item_details(conn, [(r["source"], r["source_item_id"]) for r in picks])
    out = []
    for r in picks:
        md = (details.get((r["source"], r["source_item_id"])) or {}).get("metadata", {})
        title = md.get("title") or r.get("title") or r["source_item_id"]
        item = {"source": r["source"], "source_item_id": r["source_item_id"], "item_type": domain, "title": title}
        out.append({
            "domain": domain, "kind": "item_feedback", "priority": 0.5,
            "prompt": f"Would you be into “{title}”?",
            "reason": "Muse thinks you might like it but isn't sure; your answer teaches it fastest.",
            "dedupe_key": f"item:{r['source']}:{r['source_item_id']}",
            "expires_at": datetime.now(timezone.utc) + ITEM_QUESTION_TTL,
            "context": {"item": item},
            "options": [
                {"value": "love", "label": "Love it / sounds great", "effect": [{"type": "rate", "value": 2}]},
                {"value": "like", "label": "Sure, why not", "effect": [{"type": "rate", "value": 1}]},
                {"value": "no", "label": "Not for me", "effect": [{"type": "rate", "value": -1}]},
                {"value": "never", "label": "Never suggest things like this",
                 "effect": [{"type": "rate", "value": -1},
                            {"type": "fact", "kind": "dislikes", "sentiment": -0.8, "domain": domain,
                             "statement": f"Does not want things like {title}"}]},
            ],
        })
    return out


async def _settings(conn, user_id, group) -> list[dict]:
    mine, _ = await _domain_activity(conn, group)
    total = sum(mine.values())
    out = []
    if total >= 40:
        out.append({
            "domain": "general", "kind": "scale", "priority": 0.4,
            "prompt": "How adventurous should recommendations be, from 1 (only safe bets) to 5 (surprise me)?",
            "reason": "Sets the share of explore picks Muse mixes into every list.",
            "dedupe_key": "setting:explore",
            "options": [{"value": str(k), "label": str(k), "effect": [{"type": "overlay", "explore_ratio": v}]}
                        for k, v in EXPLORE_LEVELS.items()],
        })
    strong = [d for d, n in mine.items() if n >= 30]
    weak = [d for d in ("movie", "series", "youtube_video", "audiobook") if mine.get(d, 0) < 15]
    if strong and weak:
        src = DOMAIN_LABELS[strong[0]]
        out.append({
            "domain": "general", "kind": "yes_no", "priority": 0.55,
            "prompt": f"Should Muse let your taste in {src} steer what it suggests elsewhere (films, shows, videos)?",
            "reason": f"You have a strong {src} profile and little else; cross-domain taste fills the gap.",
            "dedupe_key": "setting:cross_domain",
            "options": [
                {"value": "yes", "label": "Yes, use it", "effect": [{"type": "preference", "key": "taste_blend", "value": 0.35}]},
                {"value": "a_bit", "label": "A little", "effect": [{"type": "preference", "key": "taste_blend", "value": 0.15}]},
                {"value": "no", "label": "No, keep them separate", "effect": [{"type": "preference", "key": "taste_blend", "value": 0.0}]},
            ],
        })
    return out


GENERATORS = (_cold_start, _mixed_signals, _settings, _explore_feedback)


async def generate(conn, user_id: uuid.UUID) -> int:
    _, group = await identity_group(conn, user_id)
    n = 0
    for gen in GENERATORS:
        try:
            for q in await gen(conn, user_id, group):
                n += await _insert(conn, user_id, q)
        except Exception:
            log.exception("question generator %s failed", gen.__name__)
    return n


async def open_questions(conn, user_id: uuid.UUID, limit: int = 3, domain: str | None = None, generate_new: bool = True) -> list[dict]:
    await conn.execute(
        "UPDATE question SET status = 'expired' WHERE user_id = $1 AND status = 'open' AND expires_at < now()", user_id
    )
    sql = (
        "SELECT * FROM question WHERE user_id = $1 AND status = 'open' AND ($2::text IS NULL OR domain = $2) "
        "ORDER BY priority DESC, created_at LIMIT $3"
    )
    rows = await conn.fetch(sql, user_id, domain, limit)
    if generate_new and len(rows) < limit:
        if await generate(conn, user_id):
            rows = await conn.fetch(sql, user_id, domain, limit)
    return [question_json(r) for r in rows]


async def queue_question(conn, user_id: uuid.UUID, prompt: str, kind: str, domain: str, options: list[Any] | None,
                         reason: str | None, created_by: str, priority: float = 0.6) -> dict:
    if kind not in ("choice", "scale", "yes_no", "free_text"):
        raise HTTPException(422, "kind must be choice, scale, yes_no or free_text")
    opts = []
    for o in options or []:
        if isinstance(o, str):
            o = {"label": o}
        label = str(o.get("label") or o.get("value") or "").strip()
        if label:
            opts.append({"value": str(o.get("value") or label), "label": label})
    if kind == "yes_no" and not opts:
        opts = [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]
    if kind in ("choice", "scale") and len(opts) < 2:
        raise HTTPException(422, "choice and scale questions need at least two options")
    row = await conn.fetchrow(
        "INSERT INTO question (user_id, domain, kind, prompt, options, reason, priority, created_by) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
        user_id, domain, kind, prompt.strip(), opts, reason, max(0.0, min(1.0, priority)), created_by,
    )
    return question_json(row)


# --- answering ----------------------------------------------------------------------------


async def _rate(conn, user_id: uuid.UUID, item: dict, value: int, question_id: uuid.UUID, via: str) -> None:
    details = await item_details(conn, [(item["source"], item["source_item_id"])])
    md = dict((details.get((item["source"], item["source_item_id"])) or {}).get("metadata") or {"title": item.get("title", "")})
    md.pop("dedupe_key", None)
    md.update({"value": value, "via": f"muse-question:{via}", "question_id": str(question_id)})
    await insert_event(conn, user_id=user_id, source=item["source"], source_item_id=item["source_item_id"], item_type=item["item_type"],
                       action="rated", action_weight=RATED_WEIGHTS.get(value, 0.0), metadata=md)


async def _apply(conn, user_id, q, effects: list[dict], text: str, via: str) -> list[str]:
    done = []
    for eff in effects:
        t = eff.get("type")
        if t == "rate" and (q["context"] or {}).get("item"):
            await _rate(conn, user_id, q["context"]["item"], int(eff["value"]), q["id"], via)
            done.append(f"rated the item {eff['value']}")
        elif t == "fact":
            stmt = eff.get("statement", "{text}").replace("{text}", text).strip()
            if stmt:
                await add_fact(conn, user_id, stmt, kind=eff.get("kind", "context"), domain=eff.get("domain", q["domain"]),
                               subject=eff.get("subject"), sentiment=eff.get("sentiment"),
                               confidence=eff.get("confidence", 0.8), source="question", origin_question=q["id"])
                done.append(f"remembered: {stmt}")
        elif t == "overlay":
            await update_overlay(conn, user_id, explore_ratio=eff.get("explore_ratio"))
            done.append(f"explore ratio set to {eff.get('explore_ratio')}")
        elif t == "preference":
            await update_overlay(conn, user_id, preferences={eff["key"]: eff["value"]})
            await mark_dirty(conn, user_id)
            done.append(f"{eff['key']} set to {eff['value']}")
    return done


async def answer(conn, user_id: uuid.UUID, question_id: uuid.UUID, option: str | None, text: str | None,
                 note: str | None, via: str) -> dict:
    q = await conn.fetchrow("SELECT * FROM question WHERE id = $1 AND user_id = $2", question_id, user_id)
    if q is None:
        raise HTTPException(404, "no such question for this user")
    if q["status"] != "open":
        raise HTTPException(409, f"question is already {q['status']}")
    text = (text or "").strip()
    opts = q["options"] or []
    chosen = None
    if option is not None:
        wanted = str(option).strip().casefold()
        chosen = next((o for o in opts if o["value"].casefold() == wanted or o["label"].casefold() == wanted), None)
        if chosen is None:
            raise HTTPException(422, f"unknown option {option!r}; options are {[o['value'] for o in opts]}")
    elif not text:
        raise HTTPException(422, "answer with an option or with text")

    async with conn.transaction():
        if chosen is not None:
            effects = chosen.get("effect", [])
            if not effects:  # an assistant's own question: keep the exchange as context
                effects = [{"type": "fact", "kind": "context", "sentiment": 0.0,
                            "statement": f"Asked “{q['prompt']}”, answered “{chosen['label']}”"}]
        else:
            effects = (q["context"] or {}).get("text_effects") or [
                {"type": "fact", "kind": "context", "sentiment": 0.0, "statement": f"Asked “{q['prompt']}”, answered: {{text}}"}
            ]
        done = await _apply(conn, user_id, q, effects, text, via)
        if note:
            await add_fact(conn, user_id, note.strip(), kind="context", domain=q["domain"], sentiment=0.0,
                           source="question", origin_question=q["id"])
            done.append("kept the note")
        ans = {"option": chosen["value"] if chosen else None, "text": text or None, "note": note}
        await conn.execute(
            "UPDATE question SET status = 'answered', answered_at = now(), answer = $2, answered_via = $3 WHERE id = $1",
            question_id, ans, via,
        )
    return {"question_id": str(question_id), "applied": done}


async def skip(conn, user_id: uuid.UUID, question_id: uuid.UUID, reason: str | None) -> dict:
    row = await conn.fetchrow(
        "UPDATE question SET status = 'skipped', answered_at = now(), answer = $3 WHERE id = $1 AND user_id = $2 "
        "AND status = 'open' RETURNING id",
        question_id, user_id, {"skipped": True, "reason": reason},
    )
    if row is None:
        raise HTTPException(404, "no open question with that id")
    return {"question_id": str(question_id), "skipped": True}
