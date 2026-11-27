"""./muse setup --web: the same setup in a browser, on this machine or on your phone.

Standard library only. Starts a small server with a one-time token in the URL, prints that
URL and a QR code for it, walks through the same steps as the terminal setup (checks run
live as you type), applies them with the same code (muse.apply_setup) and ends with QR
codes for Vesper and Mono. Stops when you close it (Ctrl+C) or ten minutes after setup is done.

The token guards every request. Secrets you type travel to this machine only; over the LAN
the page is served over HTTPS with a throwaway certificate when `openssl` is available.
"""

from __future__ import annotations

import http.server
import json
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import qr  # noqa: E402

PAGE = (HERE / "web_page.html").read_text()


class State:
    def __init__(self, tool):
        self.tool = tool
        self.token = secrets.token_urlsafe(18)
        self.log: list[dict] = []
        self.running = False
        self.done = False
        self.failed = False
        self.finished_at: float | None = None
        self.lock = threading.Lock()

    def say(self, kind: str, msg: str) -> None:
        import re

        msg = re.sub(r"^\d+/\d+\s+", "", msg)  # the terminal's step numbers
        with self.lock:
            self.log.append({"kind": kind, "msg": msg, "at": time.time()})


def _detect(tool) -> dict:
    env = tool.env_values()
    docker = shutil.which("docker") is not None
    compose_ok = docker and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
    ts = tool.tailscale_identity()
    return {
        "docker": compose_ok,
        "ram_gb": round(tool.mem_gb(), 1),
        "disk_gb": round(shutil.disk_usage(tool.ROOT).free / 1e9),
        "gpu": tool.has_nvidia(),
        "lan_ip": tool.lan_ip(),
        "tailscale": {"ip": ts[0], "name": ts[1]} if ts else None,
        "timezone": env.get("MUSE_TIMEZONE") or tool.local_timezone(),
        "env": {k: v for k, v in env.items() if k in (
            "MUSE_MODE", "MUSE_TLS_HOST", "MUSE_TLS_BIND", "JELLYFIN_URL", "RADARR_URL", "SONARR_URL", "RADARR_QUALITY_PROFILE_NAME",
            "RADARR_ROOT_FOLDER", "SONARR_QUALITY_PROFILE_NAME", "SONARR_ROOT_FOLDER", "MUSE_CURATOR_BUDGET_GB", "MUSE_CURATOR_RECENT_DAYS",
            "MUSE_LLM_PROVIDER", "MUSE_LLM_BASE_URL", "MUSE_LLM_MODEL", "COMPOSE_PROFILES")},
        "have": {k: bool(tool.read_secret(k)) for k in ("jellyfin_api_key", "tmdb_api_key", "radarr_api_key", "sonarr_api_key", "muse_master_key")},
        "installed": (tool.SECRETS / "muse_master_key").exists(),
    }


def _model_for(tool) -> dict:
    gpu, gb = tool.has_nvidia(), tool.mem_gb()
    if gpu:
        return {"model": "Qwen/Qwen3-Embedding-0.6B", "device": "cuda", "note": "on your GPU", "gpu": True}
    if gb >= 12:
        return {"model": "Qwen/Qwen3-Embedding-0.6B", "device": "cpu", "note": "on the CPU, strong multilingual model", "gpu": False}
    return {"model": "intfloat/multilingual-e5-small", "device": "cpu", "note": "on the CPU, a light model for low memory", "gpu": False}


def _apply(state: State, body: dict) -> None:
    tool = state.tool
    try:
        a = body.get("answers") or {}
        provided = {k: v for k, v in (body.get("secrets") or {}).items() if v}
        # keep what is already saved when a field was left empty
        for k in ("jellyfin_api_key", "tmdb_api_key", "radarr_api_key", "sonarr_api_key"):
            if k not in provided and tool.read_secret(k):
                provided[k] = tool.read_secret(k)
        m = _model_for(tool)
        ans = {
            "mode": a.get("mode", "local"), "tls_host": a.get("tls_host", ""), "tls_bind": a.get("tls_bind", ""),
            "jellyfin_url": a.get("jellyfin_url", ""), "model": m["model"], "device": m["device"], "gpu": m["gpu"],
            "llm_provider": a.get("llm_provider", "none"), "llm_base": a.get("llm_base", ""), "llm_model": a.get("llm_model", ""),
            "llm_external": "true" if a.get("llm_external") else "false", "arr": a.get("arr") or {},
        }
        tool.apply_setup(ans, provided, start=not body.get("no_start"), log=state.say)
        state.say("ok", "Muse is running")
        state.done = True
    except SystemExit as e:  # tool.die
        state.failed = True
        state.say("fail", f"setup stopped (exit {e.code}); see the terminal for details")
    except Exception as e:  # noqa: BLE001
        state.failed = True
        state.say("fail", f"{e.__class__.__name__}: {e}")
    finally:
        state.running = False
        state.finished_at = time.time()


def make_handler(state: State):
    tool = state.tool

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "muse-setup"

        def log_message(self, *args):  # no request logs: URLs carry the token
            pass

        def _authed(self) -> bool:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cookie = self.headers.get("Cookie") or ""
            sent = (q.get("t") or [""])[0] or next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("muse_setup=")), "")
            return secrets.compare_digest(sent, state.token)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if not self._authed():
                return self._send(403, b"Open the link the terminal printed (it carries a one-time token).", "text/plain; charset=utf-8")
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8",
                                  {"Set-Cookie": f"muse_setup={state.token}; HttpOnly; SameSite=Strict; Path=/"})
            if path == "/api/state":
                return self._json({**_detect(tool), "model": _model_for(tool)})
            if path == "/api/progress":
                return self._json({"log": state.log, "running": state.running, "done": state.done, "failed": state.failed})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if not self._authed():
                return self._json({"error": "forbidden"}, 403)
            body = self._body()
            if path == "/api/check/jellyfin":
                key = body.get("key") or tool.read_secret("jellyfin_api_key")
                good, msg = tool.check_jellyfin(body.get("url", ""), key)
                return self._json({"ok": good, "msg": msg})
            if path == "/api/check/tmdb":
                key = body.get("key") or tool.read_secret("tmdb_api_key")
                return self._json({"ok": bool(key) and tool.check_tmdb(key), "msg": "TMDB key works" if key else "no key"})
            if path == "/api/check/arr":
                app = body.get("app", "radarr")
                key = body.get("key") or tool.read_secret(f"{app}_api_key")
                res = tool.check_arr(body.get("url", ""), key)
                res["suggested_budget"] = tool.suggest_budget(min((f["free_gb"] for f in res["folders"]), default=None))
                return self._json(res)
            if path == "/api/apply":
                if state.running:
                    return self._json({"error": "already running"}, 409)
                state.running, state.done, state.failed, state.log = True, False, False, []
                threading.Thread(target=_apply, args=(state, body), daemon=True).start()
                return self._json({"started": True})
            if path == "/api/pair":
                app = body.get("app", "vesper")
                try:
                    code, link, payload = tool.make_pairing(app if app != "admin" else "vesper", admin_token=bool(body.get("admin")),
                                                            ttl=int(body.get("ttl") or 15))
                except SystemExit:
                    return self._json({"error": "Muse isn't answering yet; give it a minute and try again"}, 503)
                return self._json({"code": code, "link": link, "svg": qr.to_svg(qr.encode(link, "M")),
                                   "server": payload["query_url"], "fingerprint": payload.get("ca_sha256", ""),
                                   "local_only": not tool.base_url()})
            return self._json({"error": "not found"}, 404)

    return Handler


def _self_signed(host: str) -> tuple[str, str] | None:
    if not shutil.which("openssl"):
        return None
    d = Path(tempfile.mkdtemp(prefix="muse-setup-"))
    key, crt = d / "key.pem", d / "cert.pem"
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=muse-setup",
                        "-addext", f"subjectAltName=IP:{host}" if host.replace(".", "").isdigit() else f"subjectAltName=DNS:{host}",
                        "-keyout", str(key), "-out", str(crt)], capture_output=True)
    return (str(crt), str(key)) if r.returncode == 0 else None


def serve(a, tool=None) -> None:
    if tool is None:
        tool = sys.modules.get("__main__")
    state = State(tool)
    phone = sys.stdin.isatty() and tool.yes("also open it on your phone or another computer (listens on your network while setup runs)?", default=True)
    host = tool.lan_ip() if phone else "127.0.0.1"
    port = getattr(a, "web_port", 8790) or 8790
    httpd = http.server.ThreadingHTTPServer((host, port), make_handler(state))
    scheme = "http"
    if phone:
        cert = _self_signed(host)
        if cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*cert)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        else:
            tool.warn("openssl isn't installed, so the page is plain HTTP on your network: only use it on a network you trust")
    url = f"{scheme}://{host}:{port}/?t={state.token}"
    tool.step("Muse setup in your browser")
    print(f"    {url}")
    if phone:
        print("\n    Scan to continue on your phone" + (" (accept the one-time certificate warning)" if scheme == "https" else "") + ":")
        tool.show_qr(url)
    print("    Ctrl+C stops it.")

    def stopper():
        while True:
            time.sleep(5)
            if state.finished_at and time.time() - state.finished_at > 600:
                httpd.shutdown()
                return

    threading.Thread(target=stopper, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
