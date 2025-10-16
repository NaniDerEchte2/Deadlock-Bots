# cogs/twitch/dashboard.py
import html
import logging
import re
from datetime import datetime, timezone
from typing import List, Callable, Awaitable, Optional
from urllib.parse import unquote, urlsplit, quote_plus, urlencode

from aiohttp import web

log = logging.getLogger("TwitchStreams")

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
        stats_cb: Callable[[], Awaitable[dict]],
        export_cb: Callable[[], Awaitable[dict]],
        export_csv_cb: Callable[[], Awaitable[str]],
        verify_cb: Callable[[str, str], Awaitable[str]],
    ):
        self._token = app_token
        self._noauth = noauth
        self._add = add_cb
        self._remove = remove_cb
        self._list = list_cb
        self._stats = stats_cb
        self._export = export_cb
        self._export_csv = export_csv_cb
        self._verify = verify_cb

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
  .btn-small {{ padding:.35rem .6rem; font-size:.85rem; }}
  .btn-secondary {{ background:#2a3044; color:var(--text); }}
  .btn-danger {{ background:#792e2e; }}
  table {{ width:100%; border-collapse: collapse; margin-top:1rem; }}
  th, td {{ border-bottom:1px solid var(--bd); padding:.6rem; text-align:left; }}
  th {{ color:var(--accent-2); }}
  input[type="text"] {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.45rem .6rem; border-radius:.4rem; width:28rem; }}
  small {{ color:var(--muted); }}
  .flash {{ margin:.7rem 0; padding:.5rem .7rem; border-radius:.4rem; }}
  .flash.ok {{ background:var(--ok-bg); border:1px solid var(--ok-bd); color:var(--ok-fg); }}
  .flash.err {{ background:var(--err-bg); border:1px solid var(--err-bd); color:var(--err-fg); }}
  form.inline {{ display:inline; }}
  .card-header {{ display:flex; justify-content:space-between; align-items:center; gap:.8rem; flex-wrap:wrap; }}
  .badge {{ display:inline-block; padding:.2rem .6rem; border-radius:999px; font-size:.8rem; font-weight:600; }}
  .badge-ok {{ background:var(--ok-bd); color:var(--ok-fg); }}
  .badge-warn {{ background:var(--err-bd); color:var(--err-fg); }}
  .badge-neutral {{ background:#2a3044; color:#ddd; }}
  .status-meta {{ font-size:.8rem; color:var(--muted); margin-top:.2rem; }}
  .action-stack {{ display:flex; flex-wrap:wrap; gap:.4rem; align-items:center; }}
  .countdown-ok {{ color:var(--accent-2); font-weight:600; }}
  .countdown-warn {{ color:var(--err-fg); font-weight:600; }}
  table.sortable-table th[data-sort-type] {{ cursor:pointer; user-select:none; position:relative; padding-right:1.4rem; }}
  table.sortable-table th[data-sort-type]::after {{ content:"⇅"; position:absolute; right:.4rem; color:var(--muted); font-size:.75rem; top:50%; transform:translateY(-50%); }}
  table.sortable-table th[data-sort-type][data-sort-dir="asc"]::after {{ content:"↑"; color:var(--accent-2); }}
  table.sortable-table th[data-sort-type][data-sort-dir="desc"]::after {{ content:"↓"; color:var(--accent-2); }}
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

        now = datetime.now(timezone.utc)

        def _parse_dt(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        rows: List[str] = []
        for st in items:
            login = st.get("twitch_login", "")
            login_html = html.escape(login)
            permanent = bool(st.get("manual_verified_permanent"))
            until_raw = st.get("manual_verified_until")
            until_dt = _parse_dt(until_raw)
            verified_at_dt = _parse_dt(st.get("manual_verified_at"))

            status_badge = "<span class='badge badge-neutral'>Nicht verifiziert</span>"
            meta_parts: List[str] = []
            countdown_label = "—"
            countdown_classes: List[str] = []

            if permanent:
                status_badge = "<span class='badge badge-ok'>Dauerhaft verifiziert</span>"
            elif until_dt:
                day_diff = (until_dt.date() - now.date()).days
                if day_diff >= 0:
                    status_badge = "<span class='badge badge-ok'>Verifiziert (30 Tage)</span>"
                    countdown_label = f"{day_diff} Tage"
                    countdown_classes.append("countdown-ok")
                    meta_parts.append(f"Bis {until_dt.date().isoformat()}")
                else:
                    status_badge = "<span class='badge badge-warn'>Verifizierung überfällig</span>"
                    countdown_label = f"Überfällig {abs(day_diff)} Tage"
                    countdown_classes.append("countdown-warn")
                    meta_parts.append(f"Abgelaufen am {until_dt.date().isoformat()}")

            if verified_at_dt:
                meta_parts.append(f"Bestätigt am {verified_at_dt.date().isoformat()}")

            meta_html = (
                f"<div class='status-meta'>{' • '.join(meta_parts)}</div>" if meta_parts else ""
            )

            countdown_html = html.escape(countdown_label)
            if countdown_classes:
                countdown_html = f"<span class='{' '.join(countdown_classes)}'>{countdown_html}</span>"

            escaped_login = html.escape(login, quote=True)
            actions_html = (
                "<div class='action-stack'>"
                "  <form class='inline' method='post' action='/twitch/verify'>"
                f"    <input type='hidden' name='login' value='{escaped_login}' />"
                "    <input type='hidden' name='mode' value='temp' />"
                "    <button class='btn btn-small'>30 Tage</button>"
                "  </form>"
                "  <form class='inline' method='post' action='/twitch/verify'>"
                f"    <input type='hidden' name='login' value='{escaped_login}' />"
                "    <input type='hidden' name='mode' value='permanent' />"
                "    <button class='btn btn-small'>Dauerhaft</button>"
                "  </form>"
                "  <form class='inline' method='post' action='/twitch/verify'>"
                f"    <input type='hidden' name='login' value='{escaped_login}' />"
                "    <input type='hidden' name='mode' value='clear' />"
                "    <button class='btn btn-small btn-secondary'>Reset</button>"
                "  </form>"
                "  <form class='inline' method='post' action='/twitch/remove'>"
                f"    <input type='hidden' name='login' value='{escaped_login}' />"
                "    <button class='btn btn-small btn-danger'>Remove</button>"
                "  </form>"
                "</div>"
            )

            rows.append(
                "<tr>"
                f"<td><strong>{login_html}</strong></td>"
                f"<td>{status_badge}{meta_html}</td>"
                f"<td>{countdown_html}</td>"
                f"<td>{actions_html}</td>"
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

</div>

<table>
  <thead>
    <tr><th>Login</th><th>Verifizierung</th><th>Countdown</th><th>Aktionen</th></tr>
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

    async def verify(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        mode = (data.get("mode") or "").strip().lower()
        try:
            msg = await self._verify(login, mode)
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(msg))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("dashboard verify failed: %s", e)
            raise web.HTTPFound(location="/twitch?err=" + quote_plus("Verifizierung fehlgeschlagen"))

    async def stats(self, request: web.Request):
        self._require_token(request)
        stats = await self._stats()
        tracked = stats.get("tracked", {}) or {}
        category = stats.get("category", {}) or {}

        view_mode = (request.query.get("view") or "top").lower()
        show_all = view_mode == "all"

        def build_stats_url(view: str) -> str:
            params = {}
            if view != "top":
                params["view"] = view
            if not self._noauth:
                token = request.query.get("token")
                if token:
                    params["token"] = token
            query = urlencode(params)
            return "/twitch/stats" + (f"?{query}" if query else "")

        toggle_href = build_stats_url("top" if show_all else "all")
        toggle_label = "Top 10 anzeigen" if show_all else "Alle anzeigen"
        current_view_label = "Alle" if show_all else "Top 10"

        tracked_items = tracked.get("top", []) or []
        category_items = category.get("top", []) or []
        if not show_all:
            tracked_items = tracked_items[:10]
            category_items = category_items[:10]

        def render_table(items):
            if not items:
                return "<tr><td colspan=4><i>No data yet.</i></td></tr>"
            rows = []
            for item in items:
                streamer = html.escape(str(item.get('streamer', '')))
                samples = int(item.get('samples') or 0)
                avg_viewers = float(item.get('avg_viewers') or 0.0)
                max_viewers = int(item.get('max_viewers') or 0)
                rows.append(
                    "<tr>"
                    f"<td>{streamer}</td>"
                    f"<td data-value=\"{samples}\">{samples}</td>"
                    f"<td data-value=\"{avg_viewers:.4f}\">{avg_viewers:.1f}</td>"
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>"
                    "</tr>"
                )
            return "".join(rows)

        script = """
<script>
(function () {
  const tables = document.querySelectorAll("table.sortable-table");
  tables.forEach((table) => {
    const headers = table.querySelectorAll("th[data-sort-type]");
    const tbody = table.querySelector("tbody");
    if (!tbody) {
      return;
    }
    headers.forEach((header, index) => {
      header.addEventListener("click", () => {
        const sortType = header.dataset.sortType || "string";
        const currentDir = header.dataset.sortDir === "asc" ? "desc" : "asc";
        headers.forEach((h) => h.removeAttribute("data-sort-dir"));
        header.dataset.sortDir = currentDir;
        const rows = Array.from(tbody.querySelectorAll("tr"));
        const multiplier = currentDir === "asc" ? 1 : -1;
        rows.sort((rowA, rowB) => {
          const cellA = rowA.children[index];
          const cellB = rowB.children[index];
          const rawA = cellA ? cellA.getAttribute("data-value") || cellA.textContent.trim() : "";
          const rawB = cellB ? cellB.getAttribute("data-value") || cellB.textContent.trim() : "";
          let valA = rawA;
          let valB = rawB;
          if (sortType === "number") {
            valA = Number(String(rawA).replace(/[^0-9.-]+/g, "")) || 0;
            valB = Number(String(rawB).replace(/[^0-9.-]+/g, "")) || 0;
          } else {
            valA = String(rawA).toLowerCase();
            valB = String(rawB).toLowerCase();
          }
          if (valA < valB) {
            return -1 * multiplier;
          }
          if (valA > valB) {
            return 1 * multiplier;
          }
          return 0;
        });
        rows.forEach((row) => tbody.appendChild(row));
      });
    });
  });
})();
</script>
"""

        body = f"""
<h1>📊 Stats</h1>

<div class="card">
  <h2>Deadlock Kategorie Überblick</h2>
  <p>
    Samples: {category.get('samples', 0)}<br>
    Unique Streamer: {category.get('unique_streamers', 0)}<br>
    Durchschnittliche Viewer (alle): {stats.get('avg_viewers_all', 0):.1f}<br>
    Durchschnittliche Viewer (Tracked): {stats.get('avg_viewers_tracked', 0):.1f}
  </p>
  <p>
    Tracked Samples: {tracked.get('samples', 0)} — Tracked Streamer: {tracked.get('unique_streamers', 0)}
  </p>
</div>

<div class="card" style="margin-top:1.2rem;">
  <div class="card-header">
    <h2>Top Partner Streamer (Tracked)</h2>
    <div class="row" style="gap:.6rem; align-items:center;">
      <div style="color:var(--muted); font-size:.9rem;">Ansicht: {current_view_label}</div>
      <a class="btn" href="{toggle_href}">{toggle_label}</a>
    </div>
  </div>
  <table class="sortable-table" data-table="tracked">
    <thead>
      <tr>
        <th data-sort-type="string">Streamer</th>
        <th data-sort-type="number">Samples</th>
        <th data-sort-type="number">Ø Viewer</th>
        <th data-sort-type="number">Peak Viewer</th>
      </tr>
    </thead>
    <tbody>{render_table(tracked_items)}</tbody>
  </table>
</div>

<div class="card" style="margin-top:1.2rem;">
  <div class="card-header">
    <h2>Top Deadlock Streamer (Kategorie gesamt)</h2>
    <div style="color:var(--muted); font-size:.9rem;">Ansicht: {current_view_label}</div>
  </div>
  <table class="sortable-table" data-table="category">
    <thead>
      <tr>
        <th data-sort-type="string">Streamer</th>
        <th data-sort-type="number">Samples</th>
        <th data-sort-type="number">Ø Viewer</th>
        <th data-sort-type="number">Peak Viewer</th>
      </tr>
    </thead>
    <tbody>{render_table(category_items)}</tbody>
  </table>
</div>
{script}
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
            web.post("/twitch/verify", self.verify),
            web.get("/twitch/stats", self.stats),
            web.get("/twitch/export", self.export_json),
            web.get("/twitch/export/csv", self.export_csv),
        ])
