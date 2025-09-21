# cogs/twitch_deadlock/dashboard.py
import html
import logging
import re
from typing import List, Callable, Awaitable, Optional
from urllib.parse import unquote, urlsplit, quote_plus

from aiohttp import web

log = logging.getLogger("TwitchDeadlock")

# Erlaubte Twitch-Logins: 3–25 Zeichen, a–z, 0–9, _
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")


class Dashboard:
    """
    Mini-Admin-Dashboard; kann mit oder ohne Token laufen.

    Sicherheit:
    - NoAuth: ideal nur auf 127.0.0.1 binden
    - Token-Modus: Header/Query-Token erforderlich (CSRF-Gegenmaßnahme)
    - HTML-Escaping sämtlicher Ausgaben (XSS)
    - Keine Secrets im Log (CWE-522), keine Stacktraces im HTML (CWE-209)
    """

    def __init__(
        self,
        *,
        app_token: Optional[str],
        noauth: bool,
        # add_cb soll eine Statusmeldung zurückgeben (z. B. "DisplayName hinzugefügt")
        add_cb: Callable[[str, bool], Awaitable[str]],
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

    # ---------- Auth ----------
    def _require_token(self, request: web.Request):
        if self._noauth:
            return
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if not token or not self._token or token != self._token:
            raise web.HTTPUnauthorized(text="missing or invalid token")

    # ---------- UI ----------
    def _tabs(self, active: str) -> str:
        def a(href: str, label: str, key: str) -> str:
            cls = "tab active" if key == active else "tab"
            return f'<a class="{cls}" href="{href}">{label}</a>'
        return (
            '<nav class="tabs">'
            f'{a("/twitch", "Live", "live")}'
            f'{a("/twitch/stats", "Stats", "stats")}'
            f'{a("/twitch/export", "JSON", "json")}'
            f'{a("/twitch/export/csv", "CSV", "csv")}'
            "</nav>"
        )

    def _html(self, body: str, active: str, msg: str = "", err: str = "") -> str:
        flash = ""
        if msg:
            flash = f'<div class="flash ok">{html.escape(msg)}</div>'
        elif err:
            flash = f'<div class="flash err">{html.escape(err)}</div>'
        return f"""
<!doctype html>
<meta charset="utf-8">
<title>Deadlock Twitch Posting – Admin</title>
<style>
  :root {{
    --bg:#0f0f23; --card:#151a28; --bd:#2a3044; --text:#eeeeee; --muted:#9aa4b2;
    --accent:#6d4aff; --accent-2:#9bb0ff; --ok-bg:#15391f; --ok-bd:#1d6b33; --ok-fg:#b6f0c8;
    --err-bg:#3a1a1a; --err-bd:#792e2e; --err-fg:#ffd2d2;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, Arial, sans-serif; max-width: 1000px; margin: 2rem auto; color:var(--text); background:var(--bg); }}
  .tabs {{ display:flex; gap:.5rem; margin-bottom:1rem; }}
  .tab {{ padding:.5rem .8rem; border-radius:.5rem; text-decoration:none; color:#ddd; background:#1a1f2e; border:1px solid var(--bd); }}
  .tab.active {{ background:var(--accent); color:#fff; }}
  .card {{ background:var(--card); border:1px solid var(--bd); border-radius:.7rem; padding:1rem; }}
  .row {{ display:flex; gap:1rem; align-items:center; flex-wrap:wrap; }}
  .btn {{ background:var(--accent); color:white; border:none; padding:.5rem .8rem; border-radius:.5rem; cursor:pointer; }}
  .btn:hover {{ opacity:.95; }}
  table {{ width:100%; border-collapse: collapse; margin-top:1rem; }}
  th, td {{ border-bottom:1px solid var(--bd); padding:.6rem; text-align:left; }}
  th {{ color:var(--accent-2); }}
  input[type="text"] {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.45rem .6rem; border-radius:.4rem; width:28rem; }}
  small {{ color:var(--muted); }}
  .flash {{ margin:.7rem 0; padding:.5rem .7rem; border-radius:.4rem; }}
  .flash.ok {{ background:var(--ok-bg); border:1px solid var(--ok-bd); color:var(--ok-fg); }}
  .flash.err {{ background:var(--err-bg); border:1px solid var(--err-bd); color:var(--err-fg); }}
  form.inline {{ display:inline; }}
</style>
{self._tabs(active)}
{flash}
{body}
"""

    # ---------- Helpers ----------
    @staticmethod
    def _normalize_login(value: str) -> Optional[str]:
        """
        Akzeptiert: Login, @Login, komplette/teil-URL, urlencoded etc.
        Gibt den Twitch-Login (lowercase) zurück oder None wenn ungültig.
        """
        if not value:
            return None
        s = unquote(value).strip()  # z.B. "twitch.tv%2Fxy" -> "twitch.tv/xy"
        if not s:
            return None

        # "@nick" -> "nick"
        if s.startswith("@"):
            s = s[1:].strip()

        # Wenn es wie eine URL aussieht, Pfad first-segment nehmen
        if "twitch.tv" in s or "://" in s or "/" in s:
            if "://" not in s:
                s = "https://" + s  # urlsplit braucht ein Schema
            try:
                parts = urlsplit(s)  # robustes URL-Parsing
                segs = [p for p in (parts.path or "").split("/") if p]
                if segs:
                    s = segs[0]
            except Exception:
                return None

        s = s.strip().lower()
        if LOGIN_RE.match(s):
            return s
        return None

    # ---------- Routes ----------
    async def index(self, request: web.Request):
        self._require_token(request)
        items = await self._list()

        msg = request.query.get("ok", "")
        err = request.query.get("err", "")

        rows: List[str] = []
        for st in items:
            rows.append(
                "<tr>"
                f"<td>{html.escape(st['twitch_login'])}</td>"
                f"<td>{'✅' if st['require_discord_link'] else '—'}</td>"
                f"<td>{'✅' if st['last_link_ok'] else '❌'}</td>"
                "<td>"
                "<form class='inline' method='post' action='/twitch/remove'>"
                f"<input type='hidden' name='login' value='{html.escape(st['twitch_login'])}'/>"
                "<button class='btn'>Remove</button>"
                "</form>"
                "</td>"
                "</tr>"
            )

        body = f"""
<h1 style="margin:.2rem 0 1rem 0;">Deadlock Twitch Posting – Admin</h1>

<div class="card">
  <form method="get" action="/twitch/add_any" class="row">
    <div>
      <div>Twitch Login <i>oder</i> URL:</div>
      <input name="q" placeholder="earlysalty  |  https://twitch.tv/earlysalty" required>
      <div><small>Akzeptiert: @login, login, twitch.tv/login, auch URL-encoded.</small></div>
    </div>
    <div><button class="btn">Add</button></div>
  </form>

  <form method="post" action="/twitch/rescan" style="margin-top:0.8rem">
    <button class="btn">Re-scan Discord links on all profiles</button>
  </form>
</div>

<table>
  <thead>
    <tr><th>Login</th><th>Req. Link</th><th>Has Link</th><th>Actions</th></tr>
  </thead>
  <tbody>
    {''.join(rows) or '<tr><td colspan="4"><i>Keine Streamer hinterlegt.</i></td></tr>'}
  </tbody>
</table>
"""
        return web.Response(text=self._html(body, active="live", msg=msg, err=err), content_type="text/html")

    async def _do_add(self, raw: str) -> str:
        login = self._normalize_login(raw)
        if not login:
            raise web.HTTPBadRequest(text="invalid twitch login or url")
        # UI hat keine require-link-Checkbox -> False
        msg = await self._add(login, False)
        return msg or "added"

    async def add_any(self, request: web.Request):
        """Flexible Variante: nimmt ?q= … oder ?login= … oder ?url= …"""
        self._require_token(request)
        raw = request.query.get("q") or request.query.get("login") or request.query.get("url") or ""
        try:
            msg = await self._do_add(raw)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(msg))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard add_any failed: %s", e)   # Stack nur ins Log
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("could not add (twitch api)"))

    async def add_url(self, request: web.Request):
        """Backward-compatible: nimmt ?url=… (kann jetzt auch Login enthalten)."""
        self._require_token(request)
        raw = request.query.get("url") or ""
        try:
            msg = await self._do_add(raw)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(msg))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard add_url failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("could not add (twitch api)"))

    async def add_login(self, request: web.Request):
        """Pfad-Shortcut: /twitch/add_login/<login>"""
        self._require_token(request)
        raw = request.match_info.get("login", "")
        try:
            msg = await self._do_add(raw)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(msg))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard add_login failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("could not add (twitch api)"))

    async def remove(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        try:
            await self._remove(login)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(f"{login} removed"))
        except Exception as e:
            log.exception("dashboard remove failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("could not remove"))

    async def rescan(self, request: web.Request):
        self._require_token(request)
        try:
            await self._rescan()
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus("rescan started"))
        except Exception as e:
            log.exception("dashboard rescan failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("rescan failed"))

    async def stats(self, request: web.Request):
        self._require_token(request)
        stats = await self._stats()
        rows = []
        for name, data in stats.get("top", [])[:10]:
            rows.append(f"<li>{html.escape(name)} — {int(data['sessions'])} sessions, avg {int(data['avg_viewers'])} viewers</li>")
        body = f"""
<h1>📊 Stats</h1>
<p>Total sessions: {stats.get('total_sessions', 0)} | Unique streamers: {stats.get('unique_streamers', 0)}</p>
<ol>{''.join(rows) or '<li>no data</li>'}</ol>
"""
        return web.Response(text=self._html(body, active="stats"), content_type="text/html")

    async def export_json(self, request: web.Request):
        self._require_token(request)
        data = await self._export()
        return web.json_response(data)

    async def export_csv(self, request: web.Request):
        self._require_token(request)
        data = await self._export_csv()
        return web.Response(
            text=data,
            content_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=deadlock_streams.csv"},
        )

    def attach(self, app: web.Application):
        app.add_routes([
            web.get("/twitch", self.index),
            web.get("/twitch/add_any", self.add_any),
            web.get("/twitch/add_url", self.add_url),            # kompatibel
            web.get("/twitch/add_login/{login}", self.add_login),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/rescan", self.rescan),
            web.get("/twitch/stats", self.stats),
            web.get("/twitch/export", self.export_json),
            web.get("/twitch/export/csv", self.export_csv),
        ])
