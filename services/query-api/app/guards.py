"""Small security guards for the query API."""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from fastapi import HTTPException

# Hosts people may point webhooks/calendars at even if they resolve to private addresses
# (e.g. "homeassistant.local,192.168.1.10"). Admins are always allowed.
_ALLOWED = {h.strip().lower() for h in os.environ.get("MUSE_OUTBOUND_ALLOW_PRIVATE", "").split(",") if h.strip()}


def outbound_url(url: str, admin: bool) -> None:
    """Refuse URLs that would make Muse's scheduler call into its own network (SSRF): loopback,
    private, link-local (cloud metadata), unless an admin sets them or the host is allow-listed."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(422, "url must be http(s) with a host")
    if admin or u.hostname.lower() in _ALLOWED:
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(422, f"cannot resolve {u.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise HTTPException(422, f"{u.hostname} points into a private network; ask an admin, or add it to MUSE_OUTBOUND_ALLOW_PRIVATE")


class Limiter:
    """In-memory sliding window: at most `n` events per `window` seconds per key."""

    def __init__(self, n: int, window: float) -> None:
        self.n, self.window = n, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.n:
            raise HTTPException(429, "too many attempts; try again later")

    def hit(self, key: str) -> None:
        self.hits[key].append(time.monotonic())


link_failures = Limiter(10, 3600)  # wrong link codes per caller per hour
