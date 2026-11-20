"""Context buckets: the situation an event happened in, derived the same way everywhere.

An event's `device_context` may carry {"device": "Living room TV", "device_class": "tv",
"tz": "Europe/Amsterdam", "local_hour": 21, "weekday": 5}. From that (and the timestamp when
the local time is missing) we derive coarse buckets — daypart, weekend, device class — that the
embedding worker turns into context-specific taste vectors ("track@night", "movie@weekend")
and the recommendation API blends in when the same context is current.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

DAYPARTS = ("morning", "afternoon", "evening", "night")
DEVICE_CLASSES = ("phone", "desktop", "tv", "car", "speaker", "tablet")

_DEVICE_PATTERNS = [
    ("car", r"carplay|android ?auto|\bcar\b|auto\b"),
    ("tv", r"\btv\b|television|roku|chromecast|fire ?tv|shield|apple ?tv|webos|tizen|kodi|vesper"),
    ("speaker", r"speaker|sonos|homepod|echo|nest|squeezebox"),
    ("tablet", r"ipad|tablet"),
    ("phone", r"iphone|android|phone|pixel|galaxy|mobile"),
    ("desktop", r"windows|mac|linux|desktop|laptop|web|browser|chrome|firefox|safari|electron"),
]


def device_class(ctx: dict[str, Any] | None) -> str | None:
    if not ctx:
        return None
    explicit = (ctx.get("device_class") or "").lower()
    if explicit in DEVICE_CLASSES:
        return explicit
    name = str(ctx.get("device") or ctx.get("client") or "").lower()
    for cls, pat in _DEVICE_PATTERNS:
        if name and re.search(pat, name):
            return cls
    return None


def daypart(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def local_time(ctx: dict[str, Any] | None, ts: datetime | None) -> tuple[int, int] | None:
    """(local hour, weekday 0=Mon) from the context, else from ts in ctx.tz, else None."""
    ctx = ctx or {}
    if ctx.get("local_hour") is not None:
        wd = ctx.get("weekday")
        if wd is None and ts is not None:
            wd = ts.weekday()
        return int(ctx["local_hour"]) % 24, int(wd if wd is not None else 0) % 7
    if ts is None:
        return None
    tz = ctx.get("tz")
    if tz and ZoneInfo is not None:
        try:
            ts = ts.astimezone(ZoneInfo(tz))
        except Exception:
            return None
    elif ts.tzinfo is not None and not tz:
        return None  # UTC alone says nothing about the person's clock
    return ts.hour, ts.weekday()


def buckets(ctx: dict[str, Any] | None, ts: datetime | None = None) -> list[str]:
    """Context buckets for one event or for 'now'. Empty when nothing is known."""
    out: list[str] = []
    lt = local_time(ctx, ts)
    if lt is not None:
        hour, weekday = lt
        out.append(daypart(hour))
        out.append("weekend" if weekday >= 5 else "weekday")
    dc = device_class(ctx)
    if dc:
        out.append(dc)
    return out


def bucket_key(item_type: str, bucket: str) -> str:
    """Taste-vector bucket name for a context, e.g. 'track@night'."""
    return f"{item_type}@{bucket}"


def now_context(tz: str | None = None, device: str | None = None, device_class_: str | None = None) -> dict[str, Any]:
    """A device_context describing 'now' for a request."""
    now = datetime.now(timezone.utc)
    ctx: dict[str, Any] = {}
    if tz and ZoneInfo is not None:
        try:
            local = now.astimezone(ZoneInfo(tz))
            ctx.update(tz=tz, local_hour=local.hour, weekday=local.weekday())
        except Exception:
            pass
    if device:
        ctx["device"] = device
    if device_class_:
        ctx["device_class"] = device_class_
    return ctx
