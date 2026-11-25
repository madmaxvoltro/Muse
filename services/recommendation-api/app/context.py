"""The situation a request is made in: time and device, the person's mood, and what apps,
assistants, Home Assistant, the weather and the calendar say is going on.

Precedence: explicit request parameters > the person's own signals > their room's signals >
household signals. Everything is optional; with nothing known the context is empty and
recommendations behave as before.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from muse_schema import context as C
from muse_schema import moods as M

NS = uuid.UUID("6f1b1b4a-2f2a-4b7b-9b0a-6a2c1d9e7f00")

ACTIVITY_MOODS = {"cooking": "cooking", "working": "focus", "studying": "focus", "working_out": "workout", "workout": "workout",
                  "running": "workout", "sleeping": "sleep", "going_to_sleep": "sleep", "driving": "commute", "commuting": "commute",
                  "party": "party", "dinner": "dinner", "relaxing": "chill", "reading": "focus"}
CALENDAR_MOODS = {"workout": "workout", "sport": "workout", "work": "focus", "focus": "focus", "party": "party", "dinner": "dinner",
                  "date": "romantic", "travel": "commute", "commute": "commute"}


def mood_point_id(name: str, space: str) -> str:
    return str(uuid.uuid5(NS, f"mood:{space}:{name.lower()}"))


@dataclass
class Ctx:
    buckets: list[str] = field(default_factory=list)
    mood: str | None = None  # preset name, or the custom text
    mood_def: dict = field(default_factory=dict)
    mood_text_vec: list[float] | None = None
    mood_audio_vec: list[float] | None = None
    room: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    derived: list[str] = field(default_factory=list)  # how the mood was inferred

    def summary(self) -> dict:
        return {"buckets": self.buckets, "mood": self.mood, "room": self.room, "signals": self.signals, "derived_from": self.derived}


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


async def effective_context(conn, qd, canonical: uuid.UUID | None, *, tz: str | None = None, device: str | None = None,
                            device_class: str | None = None, mood: str | None = None, room: str | None = None,
                            use_signals: bool = True) -> Ctx:
    ctx = Ctx(room=room)
    ctx.buckets = C.buckets(C.now_context(tz, device, device_class), None)
    signals: list[dict] = []
    if use_signals:
        try:
            rows = await conn.fetch(
                """
                SELECT kind, value, source, room, user_id FROM context_signal
                WHERE (expires_at IS NULL OR expires_at > now())
                  AND (user_id = $1 OR (user_id IS NULL AND (room IS NULL OR room = $2)))
                ORDER BY (user_id IS NOT NULL) DESC, (room IS NOT NULL) DESC, created_at DESC
                """,
                canonical, room,
            )
            signals = [dict(r, value=_j(r["value"])) for r in rows]
        except Exception:  # database without migration 008
            signals = []
    latest: dict[str, dict] = {}
    for s in signals:
        latest.setdefault(s["kind"], s)
    for kind in ("weather", "calendar", "activity", "presence", "location", "device"):
        if kind in latest:
            ctx.signals[kind] = latest[kind]["value"]
    if not device and "device" in latest:
        extra = C.buckets(latest["device"]["value"], None)
        ctx.buckets += [b for b in extra if b not in ctx.buckets]

    chosen, vec_t, vec_a = None, None, None
    if mood:
        chosen, ctx.derived = mood, ["request"]
    elif "mood" in latest:
        v = latest["mood"]["value"]
        chosen = v.get("mood") or v.get("text")
        vec_t, vec_a = v.get("vec_text"), v.get("vec_audio")
        ctx.derived = [f"mood set by {latest['mood']['source']}"]
    else:
        act = (ctx.signals.get("activity") or {}).get("activity")
        cal = (ctx.signals.get("calendar") or {}).get("kind")
        wx = (ctx.signals.get("weather") or {}).get("condition")
        if act and act in ACTIVITY_MOODS:
            chosen, ctx.derived = ACTIVITY_MOODS[act], [f"activity: {act}"]
        elif cal and cal in CALENDAR_MOODS:
            chosen, ctx.derived = CALENDAR_MOODS[cal], [f"calendar: {cal}"]
        elif wx in ("rain", "drizzle", "snow", "thunderstorm"):
            chosen, ctx.derived = "rainy", [f"weather: {wx}"]
    if chosen:
        name, definition = M.resolve(chosen)
        ctx.mood = name or chosen
        ctx.mood_def = definition
        ctx.mood_text_vec, ctx.mood_audio_vec = vec_t, vec_a
        if name and (vec_t is None or vec_a is None) and qd is not None:
            for space, attr, coll in (("text", "mood_text_vec", "mood_vectors"), ("audio", "mood_audio_vec", "mood_vectors_audio")):
                if getattr(ctx, attr) is None:
                    try:
                        pts = qd.retrieve(coll, [mood_point_id(name, space)], with_vectors=True)
                        if pts:
                            setattr(ctx, attr, list(pts[0].vector))
                    except Exception:
                        pass
    return ctx
