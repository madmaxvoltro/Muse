"""Muse's privacy rule for outbound calls: model endpoints inside your own network are fine;
anything else must be explicitly allowed, because prompts and item lists reveal what you own
and like."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".ts.net")


class ExternalEndpointNotAllowed(RuntimeError):
    pass


def is_private_host(host: str, resolve: bool = True) -> bool:
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or "." not in host or host.endswith(_PRIVATE_SUFFIXES):
        return True  # single-label names are docker-compose service names / LAN hosts
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")  # tailscale CGNAT
    except ValueError:
        pass
    if not resolve:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(is_private_host(a, resolve=False) for a in addrs)


def require_private_or_allowed(url: str, allow_flag: str | bool, what: str) -> None:
    allowed = allow_flag if isinstance(allow_flag, bool) else str(allow_flag).lower() in ("1", "true", "yes")
    host = urlparse(url).hostname or ""
    if not allowed and not is_private_host(host):
        raise ExternalEndpointNotAllowed(
            f"{what} {host!r} is outside your network; set the matching *_ALLOW_EXTERNAL=true to opt in"
        )
