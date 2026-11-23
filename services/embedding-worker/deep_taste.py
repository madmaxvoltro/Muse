"""Deep taste: what a person likes, understood from sound and behaviour, not only from tags.

Per person and domain (run hourly by the worker for people with new activity):

* **audio taste** — the weighted average of the CLAP vectors of what they finish and love,
  minus what they skip (collection audio_taste). Ranks by how things *sound*.
* **facets** — weighted spherical k-means over the positively weighted items, in text space
  and (tracks) in audio space. A person with two tastes gets two centres, so candidates are
  searched per facet instead of around one grey average. Audio facets are named from a
  vocabulary of sound descriptors embedded with CLAP's text encoder ("dreamy, female vocals,
  synthesizers"); text facets from genres and artists.
* **preference model** — logistic regression on their likes vs skips over audio features: what
  they pick *within* a style (energy yes, harsh brightness no). Kept only if it predicts on a
  held-out quarter (AUC), and described in plain words for the taste portrait.

Household-wide:

* **behaviour embeddings** — items played together in sessions (PPMI + SVD) in behavior_items,
  and per-person behaviour taste in behavior_taste: "people who play A also play B" for this
  household, independent of any metadata.
* **transitions** — A -> B counts with completions/skips, for next-track sequencing.
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np
from muse_schema import content_point_id, identity_group, taste_point_id
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, FieldCondition, Filter, FilterSelector, MatchValue, PointStruct, VectorParams

import taste_models as TM

log = logging.getLogger("muse.deep_taste")

AUDIO_URL = os.environ.get("MUSE_AUDIO_URL", "http://audio-analyzer:8004").rstrip("/")
LOOKBACK_DAYS = int(os.environ.get("MUSE_TASTE_LOOKBACK_DAYS", "730"))
HALFLIFE_DAYS = float(os.environ.get("MUSE_TASTE_DECAY_HALFLIFE_DAYS", "60"))
MIN_ITEMS_FOR_FACETS = 15
TEXT_DIM, AUDIO_DIM, BEHAVIOR_DIM = 384, 512, 64
NS = uuid.UUID("6f1b1b4a-2f2a-4b7b-9b0a-6a2c1d9e7f00")

COLLECTIONS = {
    "taste_facets": TEXT_DIM, "taste_facets_audio": AUDIO_DIM, "audio_taste": AUDIO_DIM,
    "behavior_items": BEHAVIOR_DIM, "behavior_taste": BEHAVIOR_DIM,
}

# Sound vocabulary for naming audio facets (CLAP text space).
DESCRIPTORS = [
    "melancholic", "euphoric", "aggressive", "dreamy", "romantic", "nostalgic", "dark and brooding", "uplifting",
    "calm and peaceful", "tense", "playful", "epic and cinematic", "sad", "happy", "angry", "sensual", "mysterious",
    "slow tempo", "fast tempo", "mid-tempo groove", "high energy", "low energy", "building intensity",
    "female vocals", "male vocals", "choir", "rap vocals", "screamed vocals", "instrumental, no vocals",
    "soft whispered vocals", "powerful belting vocals", "harmonized vocals", "spoken word",
    "acoustic guitar", "distorted electric guitars", "heavy riffs", "piano", "string orchestra", "synthesizers",
    "heavy bass", "808 drums", "live drums", "blast beats", "brass section", "saxophone", "violin", "ambient pads",
    "banjo and fiddle", "electronic beats", "hand percussion", "organ", "accordion",
    "lo-fi recording", "polished pop production", "raw garage recording", "spacious reverb", "minimal and sparse",
    "dense wall of sound", "vintage analog warmth", "crisp modern mix",
    "thrash metal", "black metal", "punk rock", "indie rock", "shoegaze", "post-rock", "dream pop", "synthwave",
    "techno", "deep house", "drum and bass", "dubstep", "trance", "hip hop beat", "trap", "r&b", "soul", "funk",
    "disco", "jazz trio", "big band jazz", "blues", "country", "folk", "singer-songwriter", "classical music",
    "opera", "film score", "ambient", "reggae", "latin rhythms", "afrobeat", "k-pop", "chanson",
    "dutch-language pop ballad", "festival anthem", "lullaby", "chiptune",
]


def ensure_collections(qd: QdrantClient) -> None:
    for name, dim in COLLECTIONS.items():
        if not qd.collection_exists(name):
            qd.create_collection(name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def facet_point_id(user: str, item_type: str, space: str, idx: int) -> str:
    return str(uuid.uuid5(NS, f"facet:{user}:{item_type}:{space}:{idx}"))


async def descriptor_vectors(http: httpx.AsyncClient | None = None) -> np.ndarray | None:
    """CLAP text vectors of DESCRIPTORS from the audio analyzer (None while it's unavailable)."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=60)
    try:
        r = await http.post(f"{AUDIO_URL}/embed/text", json={"texts": DESCRIPTORS})
        if r.status_code != 200:
            return None
        return np.array(r.json()["vectors"], dtype=np.float64)
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    finally:
        if own:
            await http.aclose()


# --- per person --------------------------------------------------------------------------------------


async def item_weights(conn, group: list[uuid.UUID], item_type: str) -> dict[tuple[str, str], float]:
    """Net, time-decayed weight per item for this person."""
    rows = await conn.fetch(
        """
        SELECT source, source_item_id, action_weight, "timestamp" FROM event
        WHERE user_id = ANY($1::uuid[]) AND item_type = $2 AND source_item_id IS NOT NULL AND action_weight <> 0
          AND "timestamp" > now() - make_interval(days => $3::int)
        """,
        group, item_type, LOOKBACK_DAYS,
    )
    now = datetime.now(timezone.utc)
    out: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        age = max((now - r["timestamp"]).total_seconds() / 86400, 0)
        out[(r["source"], r["source_item_id"])] += float(r["action_weight"]) * math.exp(-age / HALFLIFE_DAYS)
    return out


def _vectors(qd: QdrantClient, collection: str, keys: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[np.ndarray, dict]]:
    out = {}
    for i in range(0, len(keys), 256):
        chunk = keys[i:i + 256]
        ids = [content_point_id(s, sid) for s, sid in chunk]
        by_id = {content_point_id(s, sid): (s, sid) for s, sid in chunk}
        for p in qd.retrieve(collection, ids=ids, with_vectors=True, with_payload=True):
            out[by_id[str(p.id)]] = (np.array(p.vector, dtype=np.float64), p.payload or {})
    return out


def _text_label(payloads: list[dict], titles: list[str]) -> str:
    genres = Counter(g for p in payloads for g in (p.get("genres") or []) if g and g != "catalog")
    artists = Counter(t.split(" - ")[0] for t in titles if " - " in t)
    g = ", ".join(x for x, _ in genres.most_common(2))
    a = ", ".join(x for x, _ in artists.most_common(2))
    return (f"{g} ({a})" if g and a else g or a or "mixed").strip()


async def compute_person(conn, qd: QdrantClient, canonical: uuid.UUID, descriptors: np.ndarray | None) -> dict[str, Any]:
    _, group = await identity_group(conn, canonical)
    user = str(canonical)
    done: dict[str, Any] = {}
    types = [r["item_type"] for r in await conn.fetch(
        "SELECT item_type FROM event WHERE user_id = ANY($1::uuid[]) AND action_weight > 0 AND item_type <> 'search_query' "
        "GROUP BY 1 HAVING count(DISTINCT source_item_id) >= 5", group)]
    for item_type in types:
        weights = await item_weights(conn, group, item_type)
        keys = list(weights)
        text = _vectors(qd, "content_items", keys)
        audio = _vectors(qd, "audio_items", keys) if item_type == "track" else {}
        await conn.execute("DELETE FROM taste_facet WHERE user_id = $1 AND item_type = $2", canonical, item_type)
        qd.delete("taste_facets", points_selector=FilterSelector(filter=Filter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=user)), FieldCondition(key="item_type", match=MatchValue(value=item_type))])))
        qd.delete("taste_facets_audio", points_selector=FilterSelector(filter=Filter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=user)), FieldCondition(key="item_type", match=MatchValue(value=item_type))])))

        # Facets in text space.
        pos = [k for k in keys if weights[k] > 0 and k in text]
        info: dict[str, Any] = {}
        if len(pos) >= MIN_ITEMS_FOR_FACETS:
            fs = TM.facets(np.array([text[k][0] for k in pos]), np.array([weights[k] for k in pos]))
            for i, f in enumerate(fs):
                members = [pos[m] for m in f.members]
                titles = [text[k][1].get("title", "") for k in members]
                label = _text_label([text[k][1] for k in members], titles)
                await _store_facet(conn, qd, canonical, item_type, "text", i, f, label, [], members, titles, {})
            info["text_facets"] = len(fs)

        # Audio: taste vector, facets, preference model.
        if audio:
            vecs = [(audio[k][0], weights[k]) for k in keys if k in audio]
            total = sum(abs(w) for _, w in vecs)
            if total > 0:
                v = sum(vec * w for vec, w in vecs) / total
                qd.upsert("audio_taste", points=[PointStruct(id=taste_point_id(user, item_type), vector=v.tolist(),
                                                              payload={"user_id": user, "item_type": item_type, "items": len(vecs)})])
                info["audio_taste"] = len(vecs)
            apos = [k for k in keys if weights[k] > 0 and k in audio]
            if len(apos) >= MIN_ITEMS_FOR_FACETS:
                feats = await _audio_features(conn, apos)
                fs = TM.facets(np.array([audio[k][0] for k in apos]), np.array([weights[k] for k in apos]))
                for i, f in enumerate(fs):
                    members = [apos[m] for m in f.members]
                    names: list[str] = []
                    if descriptors is not None:
                        sims = descriptors @ (f.centroid / (np.linalg.norm(f.centroid) or 1))
                        names = [DESCRIPTORS[j] for j in np.argsort(-sims)[:4]]
                    avg = _avg_features([feats.get(k) for k in members])
                    label = ", ".join(names[:3]) or _text_label([audio[k][1] for k in members], [audio[k][1].get("title", "") for k in members])
                    await _store_facet(conn, qd, canonical, item_type, "audio", i, f, label, names, members,
                                       [audio[k][1].get("title", "") for k in members], avg)
                info["audio_facets"] = len(fs)
            model = await _preference(conn, weights)
            if model is not None:
                await conn.execute(
                    "INSERT INTO taste_model (user_id, item_type, kind, model, metrics) VALUES ($1, $2, 'audio_pref', $3::text::jsonb, $4::text::jsonb) "
                    "ON CONFLICT (user_id, item_type, kind) DO UPDATE SET model = EXCLUDED.model, metrics = EXCLUDED.metrics, trained_at = now()",
                    canonical, item_type, json.dumps({**model.to_json(), "summary": model.describe()}), json.dumps(model.metrics),
                )
                info["preference_auc"] = model.metrics.get("auc")

        # Behaviour taste.
        beh = _vectors(qd, "behavior_items", keys)
        bvecs = [(beh[k][0], weights[k]) for k in keys if k in beh]
        btotal = sum(abs(w) for _, w in bvecs)
        if bvecs and btotal > 0:
            bv = sum(v * w for v, w in bvecs) / btotal
            qd.upsert("behavior_taste", points=[PointStruct(id=taste_point_id(user, item_type), vector=bv.tolist(),
                                                             payload={"user_id": user, "item_type": item_type, "items": len(bvecs)})])
            info["behavior_items"] = len(bvecs)
        done[item_type] = info
    return done


async def _store_facet(conn, qd, canonical, item_type, space, idx, f, label, descriptors, members, titles, feats) -> None:
    examples = [{"source": s, "source_item_id": sid, "title": t} for (s, sid), t in zip(members[:6], titles[:6])]
    await conn.execute(
        "INSERT INTO taste_facet (user_id, item_type, space, idx, share, size, label, descriptors, examples, features) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text::jsonb, $9::text::jsonb, $10::text::jsonb)",
        canonical, item_type, space, idx, round(f.share, 4), len(members), label, json.dumps(descriptors), json.dumps(examples), json.dumps(feats),
    )
    qd.upsert("taste_facets" if space == "text" else "taste_facets_audio", points=[PointStruct(
        id=facet_point_id(str(canonical), item_type, space, idx), vector=f.centroid.tolist(),
        payload={"user_id": str(canonical), "item_type": item_type, "space": space, "idx": idx, "share": f.share, "label": label})])


async def _audio_features(conn, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    if not keys:
        return {}
    rows = await conn.fetch(
        "SELECT a.source, a.source_item_id, a.features FROM item_audio a "
        "JOIN unnest($1::text[], $2::text[]) AS k(s, i) ON a.source = k.s AND a.source_item_id = k.i WHERE a.status = 'done'",
        [k[0] for k in keys], [k[1] for k in keys],
    )
    return {(r["source"], r["source_item_id"]): _j(r["features"]) for r in rows}


def _avg_features(fs: list[dict | None]) -> dict:
    fs = [f for f in fs if f]
    if not fs:
        return {}
    out = {}
    for k in ("bpm", "energy", "danceability", "acousticness", "brightness"):
        vals = [f[k] for f in fs if isinstance(f.get(k), (int, float))]
        if vals:
            out[k] = round(float(np.mean(vals)), 3 if k != "bpm" else 1)
    modes = Counter(f.get("mode") for f in fs if f.get("mode"))
    if modes:
        out["major_share"] = round(modes.get("major", 0) / sum(modes.values()), 2)
    return out


async def _preference(conn, weights: dict[tuple[str, str], float]) -> TM.PreferenceModel | None:
    labeled = [k for k, w in weights.items() if abs(w) > 0.2]
    feats = await _audio_features(conn, labeled)
    xs, ys, sw = [], [], []
    for k in labeled:
        v = TM.feature_vector(feats.get(k) or {})
        if v is not None:
            xs.append(v)
            ys.append(1.0 if weights[k] > 0 else 0.0)
            sw.append(min(abs(weights[k]), 3.0))
    if not xs:
        return None
    model = TM.fit_preference(np.array(xs), np.array(ys), np.array(sw))
    return model if model is not None and model.metrics.get("auc", 0) >= 0.6 else None


# --- household ------------------------------------------------------------------------------------------


async def compute_household(conn, qd: QdrantClient) -> dict[str, int]:
    rows = await conn.fetch(
        """
        SELECT COALESCE(a.user_id, e.user_id)::text AS person, e.source || ':' || e.source_item_id AS item,
               extract(epoch FROM e."timestamp") AS t, e.action
        FROM event e LEFT JOIN user_alias a ON a.alias_id = e.user_id
        WHERE e.source_item_id IS NOT NULL AND e.action IN ('played', 'completed', 'skipped', 'favorited', 'added_to_playlist')
          AND e.item_type IN ('track', 'series_episode', 'youtube_video', 'movie') AND e."timestamp" > now() - make_interval(days => $1::int)
        ORDER BY 1, 3
        """,
        LOOKBACK_DAYS,
    )
    events = [(r["person"], r["item"], float(r["t"]), r["action"]) for r in rows]
    emb = TM.behavior_embeddings(TM.sessions(events))
    if emb:
        points = []
        for item, v in emb.items():
            source, sid = item.split(":", 1)
            points.append(PointStruct(id=content_point_id(source, sid), vector=v.tolist(), payload={"source": source, "source_item_id": sid}))
        for i in range(0, len(points), 500):
            qd.upsert("behavior_items", points=points[i:i + 500])
    trans = TM.transitions(events)
    async with conn.transaction():
        await conn.execute("TRUNCATE item_transition")
        if trans:
            await conn.copy_records_to_table("item_transition", records=[
                (a, b, d["count"], d["completions"], d["skips"]) for (a, b), d in trans.items()
            ], columns=["from_key", "to_key", "count", "completions", "skips"])
    return {"behavior_items": len(emb), "transitions": len(trans)}


async def run(pool, qd: QdrantClient, persons: list[str], descriptors: np.ndarray | None) -> dict:
    ensure_collections(qd)
    out: dict[str, Any] = {}
    async with pool.acquire() as conn:
        out["household"] = await compute_household(conn, qd)
        for p in persons:
            try:
                out[p] = await compute_person(conn, qd, uuid.UUID(p), descriptors)
            except Exception:
                log.exception("deep taste failed for %s", p)
    log.info("deep taste: %s", {k: v for k, v in out.items() if k == "household"} | {"people": len(persons)})
    return out
