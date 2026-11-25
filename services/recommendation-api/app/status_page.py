"""A tiny read-only status page: service health, worker progress, catalog and queue counts.
Never shows a person, a title or a key. Protected by the status token (./muse status --url);
the token is moved from the URL into an HttpOnly cookie on first visit so it doesn't linger
in browser history or proxy logs."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from muse_schema.status import collect


async def status_json(pool) -> dict:
    async with pool.acquire() as conn:
        st = await collect(conn)
    st["checked_at"] = datetime.now(timezone.utc).isoformat()
    return st


def _age(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 90:
        return f"{int(s)} s ago"
    if s < 5400:
        return f"{int(s // 60)} min ago"
    if s < 172800:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def _row(label: str, value) -> str:
    return f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"


def render(st: dict) -> str:
    w, c, q = st["worker"], st["catalog"], st["questions"]
    healthy = bool(w.get("last_cycle_at")) and (not w.get("last_error_at") or (w.get("last_cycle_at") or "") > (w.get("last_error_at") or ""))
    pct = int(100 * c["embedded"] / c["titles"]) if c["titles"] else 0
    rows_worker = "".join([
        _row("State", "running" if healthy else ("starting" if not w.get("last_cycle_at") else "last cycle failed")),
        _row("Last cycle", _age(w.get("last_cycle_at"))),
        _row("Last error", _age(w.get("last_error_at"))),
        _row("Embedding model", f"{w.get('model') or '-'} ({w.get('vector_dim') or '-'} dims)"),
        _row("Profiles refreshed", _age(w.get("last_full_profile_refresh"))),
    ])
    rows_cat = "".join([
        _row("Titles", c["titles"]), _row("In your library", c["in_library"]),
        _row("Embedded", f"{c['embedded']} ({pct}%)"), _row("Waiting for embedding", c["waiting_for_embedding"]),
        _row("Waiting for TMDB", c["waiting_for_tmdb"]),
    ])
    rows_use = "".join([
        _row("Events (24 h)", st["events"]["last_24h"]), _row("Events (total)", st["events"]["total"]),
        _row("People", st["users"]), _row("Taste profiles", st["profiles"]),
        _row("Questions waiting", q["pending"]), _row("Answered (7 days)", q["answered_7d"]),
        _row("Schema", (st["migrations"] or ["-"])[-1]),
    ])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="30">
<meta name="robots" content="noindex"><title>Muse status</title>
<style>
:root {{ --bg:#f7f7f5; --fg:#1c1c1c; --muted:#6b6b6b; --card:#fff; --line:#e6e6e2; --ok:#1f7a4d; --bad:#b3261e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --fg:#ececec; --muted:#9a9a9a; --card:#1e1e1e; --line:#2c2c2c; --ok:#5fc98f; --bad:#ff8a80; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,sans-serif; }}
main {{ max-width:880px; margin:0 auto; padding:24px 16px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .sub {{ color:var(--muted); margin:0 0 20px; }}
.grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 8px; }}
table {{ width:100%; border-collapse:collapse; }} th {{ text-align:left; font-weight:500; color:var(--muted); padding:4px 0; }}
td {{ text-align:right; padding:4px 0; font-variant-numeric:tabular-nums; }}
.pill {{ display:inline-block; padding:2px 10px; border-radius:99px; font-size:13px; color:#fff; background:{'var(--ok)' if healthy else 'var(--bad)'}; }}
</style></head><body><main>
<h1>Muse <span class="pill">{'healthy' if healthy else 'attention'}</span></h1>
<p class="sub">Checked {html.escape(st['checked_at'][:19].replace('T', ' '))} UTC. Refreshes every 30 s. No personal data is shown here.</p>
<div class="grid"><section><h2>Worker</h2><table>{rows_worker}</table></section>
<section><h2>Catalog</h2><table>{rows_cat}</table></section>
<section><h2>Activity</h2><table>{rows_use}</table></section></div>
</main></body></html>"""


async def page(request: Request, pool):
    token = request.query_params.get("token")
    if token:  # move the token into a cookie and drop it from the URL
        resp = RedirectResponse(url=str(request.url.remove_query_params("token")), status_code=303)
        resp.set_cookie("muse_status", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400)
        return resp
    return HTMLResponse(render(await status_json(pool)), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                                                  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


async def page_json(pool):
    return JSONResponse(await status_json(pool), headers={"Cache-Control": "no-store"})
