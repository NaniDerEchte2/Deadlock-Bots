# =========================================
# cogs/twitch_deadlock/dashboard.py
# =========================================
import html
import re
from typing import List, Callable, Awaitable, Optional

from aiohttp import web

DISCORD_URL_RE = re.compile(r"(?:https?://)?(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me)/[A-Za-z0-9-]+", re.I)

class Dashboard:
    """Very small admin dashboard; may run with or without an admin token.

    Security:
      - If noauth=True, bind to localhost only (recommended); otherwise require X-Admin-Token.
      - HTML-escape all dynamic values; only accept safe login patterns.
    """

    def __init__(
        self,
        *,
        app_token: Optional[str],
        noauth: bool,
        add_cb: Callable[[str, bool], Awaitable[None]],
        remove_cb: Callable[[str], Awaitable[None]],
        list_cb: Callable[[], Awaitable[List[dict]]],
        rescan_cb: Callable[[], Awaitable[None]],
        stats_cb: Callable[[], Awaitable[dict]],
        export_cb: Callable[[], Awaitable[dict]],
        export_csv_cb: Callable[[], Awaitable[str]],
    ):
        self._token = app_token
        self._noauth = noauth
        self._add = add_cb
        self._remove = remove_cb
        self._list = list_cb
        self._rescan = rescan_cb
        self._stats = stats_cb
        self._export = export_cb
        self._export_csv = export_csv_cb

    def _require_token(self, request: web.Request):
        if self._noauth:
            return
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if not token or not self._token or token != self._token:
            raise web.HTTPUnauthorized(text="missing or invalid token")

    async def index(self, request: web.Request):
        self._require_token(request)
        items = await self._list()
        rows: List[str] = []
        for st in items:
            rows.append(
                f"<tr>
<td>{html.escape(st['twitch_login'])}</td>"
                f"<td>{'✅' if st['require_discord_link'] else '—'}</td>"
                f"<td>{'✅' if st['last_link_ok'] else '❌'}</td>"
                f"<td><form method='post' action='/twitch/remove'><input type='hidden' name='login' value='{html.escape(st['twitch_login'])}'/><button>Remove</button></form></td>
</tr>"
            )
        body = f"""
<!doctype html>
<meta charset="utf-8">
<title>Twitch Deadlock – Admin</title>
<body style="font-family: system-ui; max-width: 900px; margin: 2rem auto; color:#eee; background:#0f0f23;">
<h1>Deadlock Twitch Posting – Admin</h1>
<form method="post" action="/twitch/add">
  <label>Twitch Login: <input name="login" required pattern="[A-Za-z0-9_]{{3,25}}"></label>
  <label><input type="checkbox" name="require_link" value="1"> require Discord link</label>
  <button>Add</button>
</form>
<form method="get" action="/twitch/add_url" style="margin-top:0.6rem">
  <label>twitch.tv URL: <input name="url" placeholder="https://twitch.tv/xy" size="32"></label>
  <button>Add by URL</button>
</form>
<form method="post" action="/twitch/rescan" style="margin-top:1rem">
  <button>Re-scan Discord links on all profiles</button>
</form>
<p style="margin-top:1rem"><a href="/twitch/stats">📊 Stats</a> · <a href="/twitch/export">JSON</a> · <a href="/twitch/export/csv">CSV</a></p>
<table border="1" cellspacing="0" cellpadding="6" style="margin-top:1rem; width:100%">
  <tr><th>Login</th><th>Req. Link</th><th>Has Link</th><th>Actions</th></tr>
  {''.join(rows)}
</table>
</body>
"""
        return web.Response(text=body, content_type="text/html")

    async def add(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,25}", login):
            raise web.HTTPBadRequest(text="invalid login")
        require_link = 1 if data.get("require_link") else 0
        await self._add(login, bool(require_link))
        raise web.HTTPFound(location="/twitch")

    async def add_url(self, request: web.Request):
        self._require_token(request)
        url = (request.query.get("url") or "").strip()
        m = re.search(r"twitch\.tv/([A-Za-z0-9_]{3,25})", url, flags=re.I)
        if not m:
            raise web.HTTPBadRequest(text="invalid twitch url")
        await self._add(m.group(1), False)
        raise web.HTTPFound(location="/twitch")

    async def add_login(self, request: web.Request):
        self._require_token(request)
        login = (request.match_info.get("login") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,25}", login):
            raise web.HTTPBadRequest(text="invalid login")
        await self._add(login, False)
        raise web.HTTPFound(location="/twitch")

    async def remove(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        await self._remove(login)
        raise web.HTTPFound(location="/twitch")

    async def rescan(self, request: web.Request):
        self._require_token(request)
        await self._rescan()
        raise web.HTTPFound(location="/twitch")

    async def stats(self, request: web.Request):
        self._require_token(request)
        stats = await self._stats()
        # Very simple HTML view
        rows = []
        for name, data in stats.get("top", [])[:10]:
            rows.append(f"<li>{html.escape(name)} — {int(data['sessions'])} sessions, avg {int(data['avg_viewers'])} viewers</li>")
        body = f"""
<!doctype html><meta charset="utf-8"><title>Stats</title>
<body style="font-family: system-ui; max-width: 900px; margin: 2rem auto; color:#eee; background:#0f0f23;">
<h1>📊 Deadlock Streams – Stats</h1>
<p>Total sessions: {stats.get('total_sessions', 0)} | Unique streamers: {stats.get('unique_streamers', 0)}</p>
<ol>{''.join(rows) or '<li>no data</li>'}</ol>
<p><a href="/twitch">← back</a></p>
</body>
"""
        return web.Response(text=body, content_type="text/html")

    async def export_json(self, request: web.Request):
        self._require_token(request)
        data = await self._export()
        return web.json_response(data)

    async def export_csv(self, request: web.Request):
        self._require_token(request)
        data = await self._export_csv()
        return web.Response(text=data, content_type="text/csv", headers={"Content-Disposition": "attachment; filename=deadlock_streams.csv"})

    def attach(self, app: web.Application):
        app.add_routes([
            web.get("/twitch", self.index),
            web.post("/twitch/add", self.add),
            web.get("/twitch/add_url", self.add_url),
            web.get(r"/twitch/add_login/{login}", self.add_login),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/rescan", self.rescan),
            web.get("/twitch/stats", self.stats),
            web.get("/twitch/export", self.export_json),
            web.get("/twitch/export/csv", self.export_csv),
        ])
