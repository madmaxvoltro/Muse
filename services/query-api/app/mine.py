"""A person's own data and controls: goals, imports, export and erasure, incognito, feedback
("more/less like this", "why not X?"), webhooks, the access log, a year in review, the taste
portrait, and search by sound."""

from __future__ import annotations

import json
import secrets
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from muse_schema import RATED_WEIGHTS, identity_group, taste_point_id
from muse_schema import context as C
from pydantic import BaseModel, Field
from qdrant_client.http.models import FieldCondition, Filter, FilterSelector, MatchValue

from . import importers, recclient
from .auth import Principal, admin_only, current_principal, resolve_user, writer
from .db import get_pool
from .identity import mark_dirty
from .items import DOMAINS, ITEM_TYPES, item_details, summarize_metadata
from .knowledge import add_fact
from .vectors import sound_vector, text_vector

router = APIRouter(tags=["mine"])


def _q():
    from .main import qdrant

    return qdrant()


# --- goals ------------------------------------------------------------------------------------------------


def goal_json(r) -> dict:
    c = dict(r["criteria"] or {})
    c.pop("vec", None)
    return {"id": str(r["id"]), "title": r["title"], "description": r["description"], "domain": r["domain"], "criteria": c,
            "target": r["target"], "progress": r["progress"], "status": r["status"], "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat(), "due_at": r["due_at"].isoformat() if r["due_at"] else None}


class GoalIn(BaseModel):
    title: str = Field(..., min_length=3, max_length=200, description="e.g. 'Get into jazz', 'Watch 10 classic films'")
    description: str | None = Field(None, max_length=1000)
    domain: str = "general"
    target: int = Field(10, ge=1, le=10000, description="how many matching items to finish")
    genres: list[str] = []
    artists: list[str] = []
    items: list[str] = Field([], description="explicit 'source:id' items that count")
    due_days: int | None = Field(None, ge=1, le=3650)


@router.get("/v1/users/{user}/goals")
async def goals(user: str, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        rows = await conn.fetch("SELECT * FROM goal WHERE user_id = $1 ORDER BY status = 'active' DESC, created_at DESC", uid)
    return {"goals": [goal_json(r) for r in rows]}


@router.post("/v1/users/{user}/goals")
async def add_goal(user: str, body: GoalIn, p: Principal = Depends(writer)) -> dict:
    if body.domain not in DOMAINS:
        raise HTTPException(422, f"domain must be one of {DOMAINS}")
    vec = await text_vector(f"{body.title}. {body.description or ''} {' '.join(body.genres)}")
    criteria = {"genres": body.genres, "artists": body.artists, "items": body.items, "text": body.title, "vec": vec}
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        row = await conn.fetchrow(
            "INSERT INTO goal (user_id, title, description, domain, criteria, target, created_by, due_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *",
            uid, body.title, body.description, body.domain, criteria, body.target, "assistant" if p.kind == "user" else (p.source or "user"),
            datetime.now(timezone.utc) + timedelta(days=body.due_days) if body.due_days else None)
    return goal_json(row)


class GoalPatch(BaseModel):
    status: str | None = None
    target: int | None = Field(None, ge=1, le=10000)


@router.put("/v1/users/{user}/goals/{goal_id}")
async def update_goal(user: str, goal_id: str, body: GoalPatch, p: Principal = Depends(writer)) -> dict:
    if body.status and body.status not in ("active", "paused", "done", "dropped"):
        raise HTTPException(422, "status must be active, paused, done or dropped")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        row = await conn.fetchrow("UPDATE goal SET status = COALESCE($3, status), target = COALESCE($4, target) WHERE id = $1 AND user_id = $2 RETURNING *",
                                  uuid.UUID(goal_id), uid, body.status, body.target)
    if row is None:
        raise HTTPException(404, "no such goal")
    return goal_json(row)


# --- imports -----------------------------------------------------------------------------------------------


@router.post("/v1/users/{user}/imports/{kind}")
async def import_history(user: str, kind: str, files: list[UploadFile] = File(...), p: Principal = Depends(writer)) -> dict:
    """Upload export files (or a zip of them). Re-importing the same export adds nothing."""
    if kind not in importers.KINDS:
        raise HTTPException(422, f"kind must be one of {importers.KINDS}")
    raw = []
    for f in files:
        data = await f.read()
        if len(data) > 300_000_000:
            raise HTTPException(413, f"{f.filename} is too large")
        raw.append((f.filename or "upload", data))
    try:
        events = importers.parse(kind, raw)
    except (ValueError, KeyError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"could not read that {kind} export: {e}")
    if not events:
        raise HTTPException(422, f"no {kind} history found in those files")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        existing = {r["k"] for r in await conn.fetch(
            "SELECT metadata->>'dedupe_key' AS k FROM event WHERE user_id = $1 AND source = $2 AND metadata ? 'dedupe_key'", uid, kind)}
        fresh = [e for e in events if e["metadata"]["dedupe_key"] not in existing]
        seen_keys: set[str] = set()
        records = []
        for e in fresh:
            k = e["metadata"]["dedupe_key"]
            if k in seen_keys:
                continue
            seen_keys.add(k)
            weight = e["action_weight"] if e["action_weight"] is not None else _default_weight(e["action"])
            records.append((uid, kind, e["source_item_id"], e["item_type"], e["action"], weight, e["timestamp"], e["duration_ms"],
                            e["progress_pct"], json.dumps(e["metadata"])))
        async with conn.transaction():
            for i in range(0, len(records), 5000):
                await conn.executemany(
                    "INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, \"timestamp\", duration_ms, progress_pct, metadata) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::text::jsonb)", records[i:i + 5000])
            stats = {"found": len(events), "added": len(records), "already_imported": len(events) - len(records),
                     "by_action": dict(Counter(e[4] for e in records))}
            job = await conn.fetchval("INSERT INTO import_job (user_id, kind, stats) VALUES ($1, $2, $3) RETURNING id", uid, kind, stats)
        await mark_dirty(conn, uid)
    return {"import_id": str(job), "kind": kind, **stats,
            "note": "Muse embeds the new items and updates your taste on the next worker cycles (minutes)."}


def _default_weight(action: str) -> float:
    from muse_schema import DEFAULT_ACTION_WEIGHTS, Action

    return DEFAULT_ACTION_WEIGHTS[Action(action)]


@router.get("/v1/users/{user}/imports")
async def imports(user: str, p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        rows = await conn.fetch("SELECT * FROM import_job WHERE user_id = $1 ORDER BY created_at DESC", uid)
    return {"imports": [{"id": str(r["id"]), "kind": r["kind"], "stats": r["stats"], "created_at": r["created_at"].isoformat()} for r in rows]}


# --- export, erase, incognito, access log -----------------------------------------------------------------


@router.get("/v1/users/{user}/export")
async def export(user: str, p: Principal = Depends(current_principal)) -> dict:
    """Everything Muse stores about this person (all linked ids), as JSON."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        _, group = await identity_group(conn, uid)

        async def rows(sql: str, *args) -> list[dict]:
            out = []
            for r in await conn.fetch(sql, *args):
                d = {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, uuid.UUID) else v) for k, v in dict(r).items()}
                out.append(d)
            return out

        data = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user_id": str(uid), "linked_ids": [str(g) for g in group],
            "profile": await rows("SELECT * FROM muse_user WHERE user_id = $1", uid),
            "aliases": await rows("SELECT alias_id, source, label, linked_by, created_at FROM user_alias WHERE user_id = $1", uid),
            "events": await rows("SELECT source, source_item_id, item_type, action, action_weight, \"timestamp\", duration_ms, progress_pct, metadata, device_context "
                                 "FROM event WHERE user_id = ANY($1::uuid[]) ORDER BY \"timestamp\"", group),
            "facts": await rows("SELECT id, kind, domain, statement, subject, sentiment, confidence, source, created_at, retracted_at FROM profile_fact WHERE user_id = $1", uid),
            "questions": await rows("SELECT id, domain, kind, prompt, status, answer, created_at, answered_at FROM question WHERE user_id = $1", uid),
            "settings": await rows("SELECT * FROM user_overlay WHERE user_id = $1", uid),
            "goals": await rows("SELECT id, title, description, domain, target, progress, status, created_at FROM goal WHERE user_id = $1", uid),
            "wanted": await rows("SELECT kind, artist_name, title, origin, status, created_at FROM wanted WHERE user_id = $1", uid),
            "context": await rows("SELECT kind, value - 'vec_text' - 'vec_audio' AS value, source, created_at, expires_at FROM context_signal WHERE user_id = $1", uid),
            "tokens": await rows("SELECT id, label, can_write, created_at, last_used_at, expires_at, revoked_at FROM api_token WHERE user_id = $1", uid),
            "imports": await rows("SELECT kind, stats, created_at FROM import_job WHERE user_id = $1", uid),
            "taste_facets": await rows("SELECT item_type, space, share, label, descriptors, features FROM taste_facet WHERE user_id = $1", uid),
            "taste_models": await rows("SELECT item_type, kind, model, metrics, trained_at FROM taste_model WHERE user_id = $1", uid),
            "impressions_count": await conn.fetchval("SELECT count(*) FROM impression WHERE user_id = $1", uid),
            "access_log": await rows("SELECT at, principal, method, path, status FROM access_log WHERE subject_user_id = $1 ORDER BY at DESC LIMIT 1000", uid),
        }
    return data


@router.delete("/v1/users/{user}/events")
async def forget_events(user: str, confirm: bool = False, source: str | None = None, since: datetime | None = None,
                        until: datetime | None = None, item_type: str | None = None, p: Principal = Depends(writer)) -> dict:
    """Forget history: a source, a time range ("last weekend, my nephew used my account"), a domain."""
    if not confirm:
        raise HTTPException(400, "this deletes history for good: repeat with confirm=true")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        _, group = await identity_group(conn, uid)
        res = await conn.execute(
            "DELETE FROM event WHERE user_id = ANY($1::uuid[]) AND action <> 'cataloged' AND ($2::text IS NULL OR source = $2) "
            "AND ($3::timestamptz IS NULL OR \"timestamp\" >= $3) AND ($4::timestamptz IS NULL OR \"timestamp\" < $4) AND ($5::text IS NULL OR item_type = $5)",
            group, source, since, until, item_type)
        await conn.execute("DELETE FROM impression WHERE user_id = $1 AND ($2::timestamptz IS NULL OR shown_at >= $2) AND ($3::timestamptz IS NULL OR shown_at < $3)",
                           uid, since, until)
        await mark_dirty(conn, uid)
    return {"deleted_events": int(res.split()[-1]), "note": "taste vectors are recomputed on the next worker cycle"}


@router.delete("/v1/users/{user}")
async def erase(user: str, confirm: bool = False, p: Principal = Depends(writer)) -> dict:
    """Erase a person from Muse entirely (all linked ids). Irreversible."""
    if not confirm:
        raise HTTPException(400, "this erases the person completely: repeat with confirm=true")
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        _, group = await identity_group(conn, uid)
        counts = {}
        async with conn.transaction():
            for table, col, many in (("event", "user_id", True), ("impression", "user_id", False), ("profile_fact", "user_id", False),
                                     ("question", "user_id", False), ("goal", "user_id", False), ("wanted", "user_id", False),
                                     ("context_signal", "user_id", False), ("calendar_feed", "user_id", False), ("api_token", "user_id", False),
                                     ("webhook", "user_id", False), ("taste_facet", "user_id", False), ("taste_model", "user_id", False),
                                     ("user_overlay", "user_id", False), ("room_presence", "user_id", False), ("person_ref", "user_id", False),
                                     ("import_job", "user_id", False), ("taste_dirty", "user_id", False), ("muse_user", "user_id", False)):
                ids = group if many else [uid]
                res = await conn.execute(f"DELETE FROM {table} WHERE {col} = ANY($1::uuid[])", ids)
                counts[table] = int(res.split()[-1])
            await conn.execute("DELETE FROM user_alias WHERE user_id = $1 OR alias_id = ANY($2::uuid[])", uid, group)
            await conn.execute("UPDATE group_session SET members = array_remove(members, $1)", uid)
            await conn.execute("UPDATE access_log SET subject_user_id = NULL, actor_user_id = NULL WHERE subject_user_id = $1 OR actor_user_id = $1", uid)
    qd = _q()
    for coll in ("taste_vectors", "audio_taste", "behavior_taste", "taste_facets", "taste_facets_audio"):
        try:
            qd.delete(coll, points_selector=FilterSelector(filter=Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=str(uid)))])))
        except Exception:
            pass
    return {"erased": str(uid), "linked_ids": [str(g) for g in group], "rows": counts}


class IncognitoIn(BaseModel):
    minutes: int = Field(..., ge=0, le=7 * 24 * 60, description="0 turns it off")


@router.put("/v1/users/{user}/incognito")
async def incognito(user: str, body: IncognitoIn, p: Principal = Depends(writer)) -> dict:
    """While on, the ingestion API drops this person's events (nothing is learned)."""
    until = (datetime.now(timezone.utc) + timedelta(minutes=body.minutes)).isoformat() if body.minutes else None
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        await conn.execute("INSERT INTO user_overlay (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
        await conn.execute("UPDATE user_overlay SET preferences = CASE WHEN $2::text IS NULL THEN preferences - 'incognito_until' "
                           "ELSE preferences || jsonb_build_object('incognito_until', $2::text) END WHERE user_id = $1", uid, until)
    return {"incognito_until": until}


@router.get("/v1/users/{user}/access-log")
async def access_log(user: str, limit: int = Query(200, ge=1, le=5000), p: Principal = Depends(current_principal)) -> dict:
    """Who (which token or app) read or changed this person's data, and when."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        rows = await conn.fetch(
            "SELECT l.at, l.principal, l.method, l.path, l.status, t.label FROM access_log l LEFT JOIN api_token t ON t.id = l.token_id "
            "WHERE l.subject_user_id = $1 ORDER BY l.at DESC LIMIT $2", uid, limit)
    return {"entries": [{"at": r["at"].isoformat(), "by": r["label"] or r["principal"], "method": r["method"], "path": r["path"], "status": r["status"]} for r in rows]}


@router.get("/v1/retention")
async def get_retention(p: Principal = Depends(admin_only)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return {"policies": {r["source"]: r["keep_days"] for r in await conn.fetch("SELECT * FROM retention_policy")}}


@router.put("/v1/retention")
async def set_retention(body: dict[str, int | None], p: Principal = Depends(admin_only)) -> dict:
    """{"searxng": 90, "youtube": 365, "mono": null} — null removes a policy (keep forever)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        for source, days in body.items():
            if days is None:
                await conn.execute("DELETE FROM retention_policy WHERE source = $1", source)
            elif days < 1:
                raise HTTPException(422, "keep_days must be >= 1")
            else:
                await conn.execute("INSERT INTO retention_policy VALUES ($1, $2) ON CONFLICT (source) DO UPDATE SET keep_days = $2", source, days)
        return {"policies": {r["source"]: r["keep_days"] for r in await conn.fetch("SELECT * FROM retention_policy")}}


# --- feedback ------------------------------------------------------------------------------------------------


class ItemRef(BaseModel):
    source: str
    source_item_id: str


async def _like_this(user: str, body: ItemRef, p: Principal, more: bool) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        key = (body.source, body.source_item_id)
        d = (await item_details(conn, [key])).get(key)
        if d is None:
            raise HTTPException(404, "Muse has never seen this item")
        title = d["metadata"].get("title") or body.source_item_id
        md = {k: v for k, v in d["metadata"].items() if k != "dedupe_key"} | {"value": 1 if more else -1, "via": p.source or p.kind}
        await conn.execute("INSERT INTO event (user_id, source, source_item_id, item_type, action, action_weight, metadata) VALUES ($1,$2,$3,$4,'rated',$5,$6)",
                           uid, body.source, body.source_item_id, d["item_type"], RATED_WEIGHTS[1 if more else -1], md)
        fact = await add_fact(conn, uid, f"{'More' if more else 'Less'} like {title}", "likes" if more else "dislikes", d["item_type"],
                              None, 0.6 if more else -0.6, 0.8, "user" if p.kind == "adapter" else "assistant")
    return {"title": title, "fact": fact, "note": "takes effect on the next taste update (minutes)"}


@router.post("/v1/users/{user}/more-like-this")
async def more_like_this(user: str, body: ItemRef, p: Principal = Depends(writer)) -> dict:
    return await _like_this(user, body, p, True)


@router.post("/v1/users/{user}/less-like-this")
async def less_like_this(user: str, body: ItemRef, p: Principal = Depends(writer)) -> dict:
    return await _like_this(user, body, p, False)


@router.get("/v1/users/{user}/why-not/{source}/{source_item_id}")
async def why_not(user: str, source: str, source_item_id: str, p: Principal = Depends(current_principal)) -> dict:
    """Why isn't X recommended? Seen already, filtered by something you said, a disabled source,
    not embedded yet, or simply outranked (with its rank and score breakdown)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        d = (await item_details(conn, [(source, source_item_id)])).get((source, source_item_id))
    if d is None:
        raise HTTPException(404, "Muse has never seen this item")
    try:
        return await recclient.get(f"/explain/{uid}/{source}/{source_item_id}", {"item_type": d["item_type"]})
    except recclient.RecUnavailable as e:
        raise HTTPException(503, str(e))


# --- webhooks -------------------------------------------------------------------------------------------------

EVENT_TYPES = ("new_release", "wanted", "digest", "question", "goal", "recommendation", "group", "context", "*")


class WebhookIn(BaseModel):
    url: str
    events: list[str] = Field(..., min_length=1)
    user: str | None = Field("me", description="'me', a user id, or null (admin) for household-wide")


@router.post("/v1/webhooks")
async def add_webhook(body: WebhookIn, p: Principal = Depends(writer)) -> dict:
    bad = [e for e in body.events if e not in EVENT_TYPES]
    if bad:
        raise HTTPException(422, f"unknown events {bad}; choose from {EVENT_TYPES}")
    if not body.url.startswith(("https://", "http://")):
        raise HTTPException(422, "url must be http(s)")
    secret = "whsec_" + secrets.token_urlsafe(24)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if body.user is None:
            if not p.admin:
                raise HTTPException(403, "only an admin can create household webhooks")
            uid = None
        else:
            uid = await resolve_user(conn, p, body.user)
        row = await conn.fetchrow("INSERT INTO webhook (user_id, url, secret, events) VALUES ($1,$2,$3,$4) RETURNING id", uid, body.url, secret, body.events)
    return {"id": str(row["id"]), "secret": secret, "events": body.events, "user_id": str(uid) if uid else None,
            "note": "Verify deliveries: X-Muse-Signature = 'sha256=' + hex(HMAC-SHA256(secret, raw body)). The secret is shown once."}


@router.get("/v1/webhooks")
async def list_webhooks(p: Principal = Depends(current_principal)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        me = None if p.admin else await resolve_user(conn, p, "me")
        rows = await conn.fetch("SELECT id, user_id, url, events, active, failures, created_at FROM webhook WHERE $1::uuid IS NULL OR user_id = $1", me)
    return {"webhooks": [{"id": str(r["id"]), "user_id": str(r["user_id"]) if r["user_id"] else None, "url": r["url"], "events": r["events"],
                          "active": r["active"], "failures": r["failures"]} for r in rows]}


@router.delete("/v1/webhooks/{hook_id}")
async def delete_webhook(hook_id: str, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        me = None if p.admin else await resolve_user(conn, p, "me")
        res = await conn.execute("DELETE FROM webhook WHERE id = $1 AND ($2::uuid IS NULL OR user_id = $2)", uuid.UUID(hook_id), me)
    return {"deleted": int(res.split()[-1])}


@router.post("/v1/webhooks/{hook_id}/test")
async def test_webhook(hook_id: str, p: Principal = Depends(writer)) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        me = None if p.admin else await resolve_user(conn, p, "me")
        hook = await conn.fetchrow("SELECT * FROM webhook WHERE id = $1 AND ($2::uuid IS NULL OR user_id = $2)", uuid.UUID(hook_id), me)
        if hook is None:
            raise HTTPException(404, "no such webhook")
        await conn.execute("INSERT INTO webhook_delivery (webhook_id, event, payload) VALUES ($1, 'test', $2)", hook["id"], {"hello": "from Muse"})
    return {"queued": True, "note": "delivered by the scheduler within ~10 seconds"}


# --- year in review ----------------------------------------------------------------------------------------------


@router.get("/v1/users/{user}/year-review")
async def year_review(user: str, year: int | None = None, p: Principal = Depends(current_principal)) -> dict:
    """Across everything: totals, favourites, genres, artists, discoveries, rhythm of the year."""
    year = year or datetime.now(timezone.utc).year
    start, end = datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        _, group = await identity_group(conn, uid)
        rows = await conn.fetch(
            """
            SELECT e.item_type, e.source, e.source_item_id, e.action, e.action_weight, e."timestamp", e.duration_ms, e.device_context,
                   COALESCE(NULLIF(e.metadata, '{}'::jsonb), (SELECT m.metadata FROM event m WHERE m.source = e.source AND m.source_item_id = e.source_item_id
                                                             AND m.metadata <> '{}'::jsonb LIMIT 1)) AS md
            FROM event e WHERE e.user_id = ANY($1::uuid[]) AND e."timestamp" >= $2 AND e."timestamp" < $3 AND e.action <> 'cataloged'
            """, group, start, end)
        earlier_artists = {r["a"] for r in await conn.fetch(
            "SELECT DISTINCT metadata->>'artist' AS a FROM event WHERE user_id = ANY($1::uuid[]) AND \"timestamp\" < $2 AND metadata ? 'artist'", group, start)}
    by_type: dict[str, dict[str, Any]] = defaultdict(lambda: {"events": 0, "minutes": 0.0, "items": Counter(), "genres": Counter(), "artists": Counter(),
                                                             "months": Counter(), "dayparts": Counter(), "new_artists": set(), "titles": {}})
    for r in rows:
        d = by_type[r["item_type"]]
        md = r["md"] or {}
        d["events"] += 1
        d["minutes"] += (r["duration_ms"] or 0) / 60000
        w = float(r["action_weight"] or 0)
        key = f"{r['source']}:{r['source_item_id']}"
        if w > 0:
            d["items"][key] += w
            d["titles"][key] = md.get("title") or r["source_item_id"]
            for g in md.get("genres", []) or []:
                d["genres"][g] += w
            if md.get("artist"):
                d["artists"][md["artist"]] += w
                if md["artist"] not in earlier_artists:
                    d["new_artists"].add(md["artist"])
        d["months"][r["timestamp"].strftime("%B")] += 1
        dc = r["device_context"]
        dc = json.loads(dc) if isinstance(dc, str) else dc
        for b in C.buckets(dc, r["timestamp"])[:1]:
            d["dayparts"][b] += 1
    out = {}
    for t, d in by_type.items():
        if t in ("search_query", "chat"):
            continue
        out[t] = {
            "events": d["events"], "minutes": round(d["minutes"]),
            "top": [{"title": d["titles"][k], "key": k} for k, _ in d["items"].most_common(10)],
            "top_genres": [g for g, _ in d["genres"].most_common(8)], "top_artists": [a for a, _ in d["artists"].most_common(10)],
            "new_artists": len(d["new_artists"]), "busiest_month": d["months"].most_common(1)[0][0] if d["months"] else None,
            "when": dict(d["dayparts"]),
        }
    return {"year": year, "user_id": str(uid), "domains": out}


# --- taste portrait & search by sound ------------------------------------------------------------------------------


@router.get("/v1/users/{user}/taste-portrait")
async def taste_portrait(user: str, item_type: str | None = None, p: Principal = Depends(current_principal)) -> dict:
    """What Muse understands about this person's taste beyond tags: the distinct sides of it (in
    sound and in content), what they pick within those, and how sure Muse is."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = await resolve_user(conn, p, user)
        facets = await conn.fetch("SELECT * FROM taste_facet WHERE user_id = $1 AND ($2::text IS NULL OR item_type = $2) ORDER BY item_type, space, share DESC",
                                  uid, item_type)
        models = await conn.fetch("SELECT * FROM taste_model WHERE user_id = $1 AND ($2::text IS NULL OR item_type = $2)", uid, item_type)
    domains: dict[str, Any] = defaultdict(lambda: {"sound": [], "content": [], "preferences": [], "preference_model": None})
    for f in facets:
        entry = {"label": f["label"], "share": round(float(f["share"]), 3), "size": f["size"], "examples": [e["title"] for e in (f["examples"] or [])][:4],
                 **({"sound": f["descriptors"], "audio": f["features"]} if f["space"] == "audio" else {})}
        domains[f["item_type"]]["sound" if f["space"] == "audio" else "content"].append(entry)
    for m in models:
        domains[m["item_type"]]["preferences"] = (m["model"] or {}).get("summary", [])
        domains[m["item_type"]]["preference_model"] = m["metrics"]
    qd = _q()
    for t, d in domains.items():
        parts = []
        sides = d["sound"] or d["content"]
        if len(sides) > 1:
            parts.append(f"{len(sides)} sides to your {t if t != 'track' else 'music'}: " + "; ".join(f"{s['label']} ({round(s['share'] * 100)}%)" for s in sides))
        elif sides:
            parts.append(f"Mostly {sides[0]['label']}")
        if d["preferences"]:
            parts.append("Within that you prefer " + ", ".join(x.removeprefix("prefers ") for x in d["preferences"]))
        d["summary"] = ". ".join(parts) + ("." if parts else "")
        d["understood_by"] = [name for name, coll in (("sound", "audio_taste"), ("behaviour", "behavior_taste")) if _has_point(qd, coll, taste_point_id(str(uid), t))]
    return {"user_id": str(uid), "domains": dict(domains),
            "note": "Recomputed hourly from what you finish, love and skip. Sound labels come from an audio model, not from tags."}


def _has_point(qd, coll: str, pid: str) -> bool:
    try:
        return bool(qd.retrieve(coll, [pid]))
    except Exception:
        return False


@router.get("/v1/search/sound")
async def search_sound(q: str = Query(..., min_length=2, max_length=300, description="describe the sound: 'dreamy synths, slow, female vocals'"),
                       limit: int = Query(20, ge=1, le=100), source: list[str] | None = Query(None), user: str | None = "me",
                       p: Principal = Depends(current_principal)) -> dict:
    """Search music by how it sounds (CLAP), optionally leaning towards the person's taste in sound."""
    vec = await sound_vector(q)
    if vec is None:
        raise HTTPException(503, "search by sound needs the audio analyzer with its model")
    from qdrant_client.http.models import MatchAny

    qd = _q()
    flt = Filter(must=[FieldCondition(key="source", match=MatchAny(any=source))]) if source else None
    pool = await get_pool()
    async with pool.acquire() as conn:
        uid = None if (p.admin and user in (None, "", "me")) else await resolve_user(conn, p, user)
        taste = None
        if uid is not None:
            pts = qd.retrieve("audio_taste", [taste_point_id(str(uid), "track")], with_vectors=True) if _has_point(qd, "audio_taste", taste_point_id(str(uid), "track")) else []
            taste = list(pts[0].vector) if pts else None
        hits = qd.query_points("audio_items", query=vec, query_filter=flt, limit=limit * 2, with_vectors=taste is not None).points
        details = await item_details(conn, [((h.payload or {}).get("source"), (h.payload or {}).get("source_item_id")) for h in hits])
    import math

    def cosv(a, b):
        return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) or 1) * (math.sqrt(sum(x * x for x in b)) or 1))

    out = []
    for h in hits:
        pl = h.payload or {}
        k = (pl.get("source"), pl.get("source_item_id"))
        fit = cosv(taste, h.vector) if taste is not None and h.vector is not None else None
        d = details.get(k, {"item_type": "track", "metadata": {"title": pl.get("title")}})
        out.append({"source": k[0], "source_item_id": k[1], **summarize_metadata("track", d["metadata"]),
                    "audio": {x: pl[x] for x in ("bpm", "energy", "camelot", "danceability") if x in pl},
                    "match": round(float(h.score), 4), **({"taste_fit": round(fit, 4)} if fit is not None else {}),
                    "score": round(float(h.score) * (0.75 + 0.25 * (fit if fit is not None else 0.5)), 4)})
    out.sort(key=lambda r: -r["score"])
    return {"query": q, "results": out[:limit]}
