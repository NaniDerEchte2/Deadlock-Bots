# =========================================
# cogs/twitch_deadlock/dashboard.py
# =========================================
import html
import re
from typing import List

from aiohttp import web

DISCORD_URL_RE = re.compile(r"(?:https?://)?(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me)/[A-Za-z0-9-]+", re.I)

class Dashboard:
    """Very small admin dashboard protected by a static token.

    Security (CWE):
      - CSRF: demand token on every POST (CWE-352)
      - XSS: HTML-escape all dynamic values (CWE-79)
    """

    def __init__(self, app_token: str, add_cb, remove_cb, list_cb, rescan_cb):
        self._token = app_token
        self._add = add_cb
        self._remove = remove_cb
        self._list = list_cb
        self._rescan = rescan_cb

    def _require_token(self, request: web.Request):
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if not token or token != self._token:
            raise web.HTTPUnauthorized(text="missing or invalid token")

    async def index(self, request: web.Request):
        self._require_token(request)
        items = await self._list()
        rows: List[str] = []
        for st in items:
            rows.append(
                f"<tr>\n<td>{html.escape(st['twitch_login'])}</td>"
                f"<td>{'✅' if st['require_discord_link'] else '—'}</td>"
                f"<td>{'✅' if st['last_link_ok'] else '❌'}</td>"
                f"<td><form method='post' action='/twitch/remove?token={html.escape(self._token)}' style='display:inline'>"
                f"<input type='hidden' name='login' value='{html.escape(st['twitch_login'])}'/>"
                f"<button>Remove</button></form></td>\n</tr>"
            )
        body = f"""
<!doctype html>
<meta charset="utf-8">
<title>Twitch Deadlock – Admin</title>
<body style="font-family: system-ui; max-width: 900px; margin: 2rem auto;">
<h1>Deadlock Twitch Posting – Admin</h1>
<form method="post" action="/twitch/add?token={html.escape(self._token)}">
  <label>Twitch Login: <input name="login" required></label>
  <label><input type="checkbox" name="require_link" value="1"> require Discord link</label>
  <button>Add</button>
</form>
<form method="post" action="/twitch/rescan?token={html.escape(self._token)}" style="margin-top:1rem">
  <button>Re-scan Discord links on all profiles</button>
</form>
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
        raise web.HTTPFound(location=f"/twitch?token={self._token}")

    async def remove(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        await self._remove(login)
        raise web.HTTPFound(location=f"/twitch?token={self._token}")

    async def rescan(self, request: web.Request):
        self._require_token(request)
        await self._rescan()
        raise web.HTTPFound(location=f"/twitch?token={self._token}")

    def attach(self, app: web.Application):
        app.add_routes([
            web.get("/twitch", self.index),
            web.post("/twitch/add", self.add),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/rescan", self.rescan),
        ])
