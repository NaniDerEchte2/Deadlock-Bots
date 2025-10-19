# cogs/twitch/dashboard.py
import html
import json
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
        partner_token: Optional[str],
        # add_cb soll eine Statusmeldung zurückgeben (z. B. "DisplayName hinzugefügt")
        add_cb: Callable[[str, bool], Awaitable[str]],
        remove_cb: Callable[[str], Awaitable[None]],
        list_cb: Callable[[], Awaitable[List[dict]]],
        stats_cb: Callable[..., Awaitable[dict]],
        verify_cb: Callable[[str, str], Awaitable[str]],
    ):
        self._token = app_token
        self._noauth = noauth
        self._partner_token = partner_token
        self._add = add_cb
        self._remove = remove_cb
        self._list = list_cb
        self._stats = stats_cb
        self._verify = verify_cb

    # ---------- Auth ----------
    def _require_token(self, request: web.Request):
        if self._noauth:
            return
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if not token or not self._token or token != self._token:
            raise web.HTTPUnauthorized(text="missing or invalid token")

    def _require_partner_token(self, request: web.Request):
        if self._noauth:
            return
        partner_header = request.headers.get("X-Partner-Token")
        partner_query = request.query.get("partner_token")
        admin_header = request.headers.get("X-Admin-Token")
        admin_query = request.query.get("token")

        if self._partner_token:
            if partner_header == self._partner_token or partner_query == self._partner_token:
                return
            if self._token and (admin_header == self._token or admin_query == self._token):
                return
            raise web.HTTPUnauthorized(text="missing or invalid partner token")

        # Fallback: wenn kein Partner-Token gesetzt ist, gilt das Admin-Token
        self._require_token(request)

    # ---------- UI ----------
    def _tabs(self, active: str) -> str:
        def a(href: str, label: str, key: str) -> str:
            cls = "tab active" if key == active else "tab"
            return f'<a class="{cls}" href="{href}">{label}</a>'
        return (
            '<nav class="tabs">'
            f'{a("/twitch", "Live", "live")}'
            f'{a("/twitch/stats", "Stats", "stats")}'
            "</nav>"
        )

    def _html(
        self,
        body: str,
        active: str,
        msg: str = "",
        err: str = "",
        nav: Optional[str] = None,
    ) -> str:
        flash = ""
        if msg:
            flash = f'<div class="flash ok">{html.escape(msg)}</div>'
        elif err:
            flash = f'<div class="flash err">{html.escape(err)}</div>'
        nav_html = self._tabs(active) if nav is None else nav
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
  input[type="number"], select {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.45rem .6rem; border-radius:.4rem; }}
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
  .filter-form {{ margin-top:.6rem; }}
  .filter-form .row {{ align-items:flex-end; gap:1rem; }}
  .filter-label {{ display:flex; flex-direction:column; gap:.3rem; font-size:.9rem; color:var(--muted); }}
  .chart-panel {{ background:#10162a; border:1px solid var(--bd); border-radius:.7rem; padding:1rem; margin-top:1rem; }}
  .chart-panel h3 {{ margin:0 0 .6rem 0; font-size:1.1rem; color:var(--accent-2); }}
  .chart-panel canvas {{ width:100%; height:320px; max-height:360px; }}
  .chart-note {{ margin-top:.6rem; font-size:.85rem; color:var(--muted); }}
  .chart-empty {{ margin-top:1rem; font-size:.9rem; color:var(--muted); font-style:italic; }}
</style>
{nav_html}
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
            msg = await self._remove(login)
            message = msg or f"{login} removed"
            raise web.HTTPFound(location="/twitch?ok=" + quote_plus(message))
        except web.HTTPException:
            raise
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

    async def _render_stats_page(self, request: web.Request, *, partner_view: bool) -> web.Response:
        view_mode = (request.query.get("view") or "top").lower()
        show_all = view_mode == "all"

        def _parse_int(*names: str) -> Optional[int]:
            for name in names:
                raw = request.query.get(name)
                if raw is None or raw == "":
                    continue
                try:
                    value = int(raw)
                except ValueError:
                    continue
                return max(0, value)
            return None

        def _parse_float(*names: str) -> Optional[float]:
            for name in names:
                raw = request.query.get(name)
                if raw is None or raw == "":
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue
                return max(0.0, value)
            return None

        def _clamp_hour(value: Optional[int]) -> Optional[int]:
            if value is None:
                return None
            if value < 0:
                return 0
            if value > 23:
                return 23
            return value

        stats_hour = _clamp_hour(_parse_int("hour"))
        hour_from = _clamp_hour(_parse_int("hour_from", "from_hour", "start_hour"))
        hour_to = _clamp_hour(_parse_int("hour_to", "to_hour", "end_hour"))

        if stats_hour is not None:
            if hour_from is None:
                hour_from = stats_hour
            if hour_to is None:
                hour_to = stats_hour

        stats = await self._stats(hour_from=hour_from, hour_to=hour_to)
        tracked = stats.get("tracked", {}) or {}
        category = stats.get("category", {}) or {}

        min_samples = _parse_int("min_samples", "samples")
        min_avg = _parse_float("min_avg", "avg")
        partner_filter = (request.query.get("partner") or "any").lower()
        if partner_filter not in {"only", "exclude", "any"}:
            partner_filter = "any"

        base_path = request.rel_url.path

        preserved_params = {}
        if not self._noauth:
            admin_token = request.query.get("token")
            if admin_token:
                preserved_params["token"] = admin_token
        partner_token = request.query.get("partner_token")
        if partner_token:
            preserved_params["partner_token"] = partner_token

        filter_params = {}
        if min_samples is not None:
            filter_params["min_samples"] = str(min_samples)
        if min_avg is not None:
            filter_params["min_avg"] = f"{min_avg:g}"
        if partner_filter in {"only", "exclude"}:
            filter_params["partner"] = partner_filter
        if hour_from is not None:
            filter_params["hour_from"] = str(hour_from)
        if hour_to is not None:
            filter_params["hour_to"] = str(hour_to)

        def build_stats_url(view: str) -> str:
            params = dict(preserved_params)
            params.update(filter_params)
            if view != "top":
                params["view"] = view
            else:
                params.pop("view", None)
            query = urlencode(params)
            return base_path + (f"?{query}" if query else "")

        toggle_href = build_stats_url("top" if show_all else "all")
        toggle_label = "Top 10 anzeigen" if show_all else "Alle anzeigen"
        current_view_label = "Alle" if show_all else "Top 10"

        def apply_filters(items: List[dict]) -> List[dict]:
            result: List[dict] = []
            for item in items:
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                is_partner = bool(item.get("is_partner"))
                if min_samples is not None and samples < min_samples:
                    continue
                if min_avg is not None and avg_viewers < min_avg:
                    continue
                if partner_filter == "only" and not is_partner:
                    continue
                if partner_filter == "exclude" and is_partner:
                    continue
                result.append(item)
            return result

        tracked_items = apply_filters(tracked.get("top", []) or [])
        category_items = apply_filters(category.get("top", []) or [])
        if not show_all:
            tracked_items = tracked_items[:10]
            category_items = category_items[:10]

        def render_table(items: List[dict]) -> str:
            if not items:
                return "<tr><td colspan=5><i>Keine Daten für die aktuellen Filter.</i></td></tr>"
            rows = []
            for item in items:
                streamer = html.escape(str(item.get("streamer", "")))
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                max_viewers = int(item.get("max_viewers") or 0)
                is_partner = bool(item.get("is_partner"))
                partner_text = "Ja" if is_partner else "Nein"
                partner_value = "1" if is_partner else "0"
                rows.append(
                    "<tr>"
                    f"<td>{streamer}</td>"
                    f"<td data-value=\"{samples}\">{samples}</td>"
                    f"<td data-value=\"{avg_viewers:.4f}\">{avg_viewers:.1f}</td>"
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>"
                    f"<td data-value=\"{partner_value}\">{partner_text}</td>"
                    "</tr>"
                )
            return "".join(rows)

        tracked_hourly = tracked.get("hourly", []) or []
        category_hourly = category.get("hourly", []) or []
        tracked_weekday = tracked.get("weekday", []) or []
        category_weekday = category.get("weekday", []) or []

        def _format_float(value: float) -> str:
            return f"{value:.1f}"

        def _float_or_none(value, *, digits: int = 1):
            if value is None:
                return None
            try:
                return round(float(value), digits)
            except (TypeError, ValueError):
                return None

        def _int_or_none(value):
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def render_hour_table(items: List[dict]) -> str:
            if not items:
                return "<tr><td colspan=4><i>Keine Daten verfügbar.</i></td></tr>"
            rows = []
            for item in sorted(items, key=lambda d: int(d.get("hour") or 0)):
                hour = int(item.get("hour") or 0)
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                max_viewers = int(item.get("max_viewers") or 0)
                rows.append(
                    "<tr>"
                    f"<td data-value=\"{hour}\">{hour:02d}:00</td>"
                    f"<td data-value=\"{samples}\">{samples}</td>"
                    f"<td data-value=\"{avg_viewers:.4f}\">{_format_float(avg_viewers)}</td>"
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>"
                    "</tr>"
                )
            return "".join(rows)

        weekday_labels = {
            0: "Sonntag",
            1: "Montag",
            2: "Dienstag",
            3: "Mittwoch",
            4: "Donnerstag",
            5: "Freitag",
            6: "Samstag",
        }
        weekday_order = [1, 2, 3, 4, 5, 6, 0]

        def render_weekday_table(items: List[dict]) -> str:
            if not items:
                return "<tr><td colspan=4><i>Keine Daten verfügbar.</i></td></tr>"
            by_day = {int(d.get("weekday") or 0): d for d in items}
            rows = []
            for idx in weekday_order:
                item = by_day.get(idx)
                if not item:
                    continue
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                max_viewers = int(item.get("max_viewers") or 0)
                label = weekday_labels.get(idx, str(idx))
                rows.append(
                    "<tr>"
                    f"<td data-value=\"{idx}\">{html.escape(label)}</td>"
                    f"<td data-value=\"{samples}\">{samples}</td>"
                    f"<td data-value=\"{avg_viewers:.4f}\">{_format_float(avg_viewers)}</td>"
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>"
                    "</tr>"
                )
            if not rows:
                return "<tr><td colspan=4><i>Keine Daten verfügbar.</i></td></tr>"
            return "".join(rows)

        category_hour_rows = render_hour_table(category_hourly)
        tracked_hour_rows = render_hour_table(tracked_hourly)
        category_weekday_rows = render_weekday_table(category_weekday)
        tracked_weekday_rows = render_weekday_table(tracked_weekday)

        category_hour_map = {
            int(item.get("hour") or 0): item for item in category_hourly if isinstance(item, dict)
        }
        tracked_hour_map = {
            int(item.get("hour") or 0): item for item in tracked_hourly if isinstance(item, dict)
        }

        def _build_dataset(
            data_points,
            *,
            label: str,
            color: str,
            background: str,
            axis: str = "yAvg",
            fill: bool = True,
            dash: Optional[List[int]] = None,
            tension: float = 0.35,
        ) -> Optional[dict]:
            if not data_points or not any(value is not None for value in data_points):
                return None
            dataset = {
                "label": label,
                "data": data_points,
                "borderColor": color,
                "backgroundColor": background,
                "fill": fill,
                "tension": tension,
                "spanGaps": True,
                "borderWidth": 2,
                "yAxisID": axis,
                "pointRadius": 3,
                "pointHoverRadius": 4,
            }
            if dash:
                dataset["borderDash"] = dash
            return dataset

        hour_labels = [f"{hour:02d}:00" for hour in range(24)]
        category_hour_avg = [
            _float_or_none((category_hour_map.get(hour) or {}).get("avg_viewers")) for hour in range(24)
        ]
        tracked_hour_avg = [
            _float_or_none((tracked_hour_map.get(hour) or {}).get("avg_viewers")) for hour in range(24)
        ]
        category_hour_peak = [
            _int_or_none((category_hour_map.get(hour) or {}).get("max_viewers")) for hour in range(24)
        ]
        tracked_hour_peak = [
            _int_or_none((tracked_hour_map.get(hour) or {}).get("max_viewers")) for hour in range(24)
        ]

        hour_datasets = [
            ds
            for ds in (
                _build_dataset(
                    category_hour_avg,
                    label="Kategorie Ø Viewer",
                    color="#6d4aff",
                    background="rgba(109, 74, 255, 0.25)",
                ),
                _build_dataset(
                    tracked_hour_avg,
                    label="Tracked Ø Viewer",
                    color="#4adede",
                    background="rgba(74, 222, 222, 0.2)",
                ),
                _build_dataset(
                    category_hour_peak,
                    label="Kategorie Peak Viewer",
                    color="#ffb347",
                    background="rgba(255, 179, 71, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[6, 4],
                    tension=0.25,
                ),
                _build_dataset(
                    tracked_hour_peak,
                    label="Tracked Peak Viewer",
                    color="#ff6f91",
                    background="rgba(255, 111, 145, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[4, 4],
                    tension=0.25,
                ),
            )
            if ds
        ]

        category_weekday_map = {
            int(item.get("weekday") or 0): item for item in category_weekday if isinstance(item, dict)
        }
        tracked_weekday_map = {
            int(item.get("weekday") or 0): item for item in tracked_weekday if isinstance(item, dict)
        }

        weekday_labels_list = [weekday_labels.get(idx, str(idx)) for idx in weekday_order]
        category_weekday_avg = [
            _float_or_none((category_weekday_map.get(idx) or {}).get("avg_viewers"))
            for idx in weekday_order
        ]
        tracked_weekday_avg = [
            _float_or_none((tracked_weekday_map.get(idx) or {}).get("avg_viewers"))
            for idx in weekday_order
        ]
        category_weekday_peak = [
            _int_or_none((category_weekday_map.get(idx) or {}).get("max_viewers"))
            for idx in weekday_order
        ]
        tracked_weekday_peak = [
            _int_or_none((tracked_weekday_map.get(idx) or {}).get("max_viewers"))
            for idx in weekday_order
        ]

        weekday_datasets = [
            ds
            for ds in (
                _build_dataset(
                    category_weekday_avg,
                    label="Kategorie Ø Viewer",
                    color="#6d4aff",
                    background="rgba(109, 74, 255, 0.25)",
                ),
                _build_dataset(
                    tracked_weekday_avg,
                    label="Tracked Ø Viewer",
                    color="#4adede",
                    background="rgba(74, 222, 222, 0.2)",
                ),
                _build_dataset(
                    category_weekday_peak,
                    label="Kategorie Peak Viewer",
                    color="#ffb347",
                    background="rgba(255, 179, 71, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[6, 4],
                    tension=0.25,
                ),
                _build_dataset(
                    tracked_weekday_peak,
                    label="Tracked Peak Viewer",
                    color="#ff6f91",
                    background="rgba(255, 111, 145, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[4, 4],
                    tension=0.25,
                ),
            )
            if ds
        ]

        hour_chart_block = (
            "<div class=\"chart-panel\">"
            "  <h3>Viewer nach Stunde (UTC)</h3>"
            "  <canvas id=\"hourly-viewers-chart\" height=\"320\"></canvas>"
            "  <div class=\"chart-note\">Durchschnitt (gefüllt) und Peak (gestrichelt).</div>"
            "</div>"
            if hour_datasets
            else "<div class=\"chart-empty\">Noch keine Stunden-Daten vorhanden.</div>"
        )

        weekday_chart_block = (
            "<div class=\"chart-panel\">"
            "  <h3>Viewer nach Wochentag</h3>"
            "  <canvas id=\"weekday-viewers-chart\" height=\"320\"></canvas>"
            "  <div class=\"chart-note\">Vergleich von Ø und Peak Viewer je Tag.</div>"
            "</div>"
            if weekday_datasets
            else "<div class=\"chart-empty\">Noch keine Wochentags-Daten vorhanden.</div>"
        )

        chart_payload = {
            "hour": {
                "labels": hour_labels,
                "datasets": hour_datasets,
                "xTitle": "Stunde (UTC)",
            },
            "weekday": {
                "labels": weekday_labels_list,
                "datasets": weekday_datasets,
                "xTitle": "Wochentag",
            },
        }

        chart_payload_json = json.dumps(chart_payload, ensure_ascii=False)

        script = """
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
(function () {
  const chartData = __CHART_DATA__;

  function hasRenderableData(dataset) {
    if (!dataset || !Array.isArray(dataset.data)) {
      return false;
    }
    return dataset.data.some((value) => value !== null && value !== undefined);
  }

  function renderLineChart(config) {
    if (typeof Chart === "undefined") {
      return;
    }
    const canvas = document.getElementById(config.id);
    if (!canvas) {
      return;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      return;
    }
    const datasets = (config.data.datasets || [])
      .filter((dataset) => hasRenderableData(dataset))
      .map((dataset) => ({
        ...dataset,
        data: dataset.data.map((value) =>
          value === null || value === undefined ? null : Number(value)
        ),
      }));
    if (!datasets.length) {
      return;
    }
    const gridColor = "rgba(154, 164, 178, 0.2)";
    const options = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: { color: "#dddddd" },
        },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              const value = ctx.parsed.y;
              if (value === null || value === undefined || Number.isNaN(value)) {
                return ctx.dataset.label + ": –";
              }
              const isAverage = /Ø/.test(ctx.dataset.label);
              const digits = isAverage ? 1 : 0;
              return (
                ctx.dataset.label +
                ": " +
                Number(value).toLocaleString("de-DE", {
                  minimumFractionDigits: digits,
                  maximumFractionDigits: digits,
                })
              );
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: "#dddddd" },
          grid: { color: gridColor },
        },
        yAvg: {
          type: "linear",
          position: "left",
          ticks: { color: "#dddddd" },
          grid: { color: gridColor },
          title: { display: true, text: "Ø Viewer", color: "#9bb0ff" },
        },
      },
      elements: {
        point: {
          hitRadius: 6,
        },
      },
    };

    if (config.data && config.data.xTitle) {
      options.scales.x.title = {
        display: true,
        text: config.data.xTitle,
        color: "#dddddd",
      };
    }

    const hasPeakDataset = datasets.some((dataset) => dataset.yAxisID === "yPeak");
    if (hasPeakDataset) {
      options.scales.yPeak = {
        type: "linear",
        position: "right",
        ticks: { color: "#dddddd" },
        grid: { drawOnChartArea: false },
        title: { display: true, text: "Peak Viewer", color: "#ffb347" },
      };
    }

    new Chart(ctx, {
      type: "line",
      data: {
        labels: config.data.labels || [],
        datasets,
      },
      options,
    });
  }

  if (
    chartData.hour &&
    Array.isArray(chartData.hour.datasets) &&
    chartData.hour.datasets.length
  ) {
    renderLineChart({ id: "hourly-viewers-chart", data: chartData.hour });
  }

  if (
    chartData.weekday &&
    Array.isArray(chartData.weekday.datasets) &&
    chartData.weekday.datasets.length
  ) {
    renderLineChart({ id: "weekday-viewers-chart", data: chartData.weekday });
  }

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
""".replace("__CHART_DATA__", chart_payload_json)

        filter_descriptions = []
        if min_samples is not None:
            filter_descriptions.append(f"Samples ≥ {min_samples}")
        if min_avg is not None:
            filter_descriptions.append(f"Ø Viewer ≥ {min_avg:.1f}")
        if partner_filter == "only":
            filter_descriptions.append("Nur Partner")
        elif partner_filter == "exclude":
            filter_descriptions.append("Ohne Partner")
        if hour_from is not None or hour_to is not None:
            start = hour_from if hour_from is not None else hour_to
            end = hour_to if hour_to is not None else hour_from
            if start is None:
                start = 0
            if end is None:
                end = start
            if start == end:
                filter_descriptions.append(f"Stunde {start:02d} UTC")
            else:
                wrap_hint = " (über Mitternacht)" if start > end else ""
                filter_descriptions.append(f"Stunden {start:02d}–{end:02d} UTC{wrap_hint}")
        if not filter_descriptions:
            filter_descriptions.append("Keine Filter aktiv")

        hidden_inputs = []
        for key, value in preserved_params.items():
            hidden_inputs.append(
                f"<input type='hidden' name='{html.escape(key)}' value='{html.escape(value)}'>"
            )
        if show_all:
            hidden_inputs.append("<input type='hidden' name='view' value='all'>")
        hidden_inputs_html = "".join(hidden_inputs)

        clear_params = dict(preserved_params)
        if show_all:
            clear_params["view"] = "all"
        clear_query = urlencode(clear_params)
        clear_url = base_path + (f"?{clear_query}" if clear_query else "")

        partner_select_options = {
            "any": "Alle",
            "only": "Nur Partner",
            "exclude": "Ohne Partner",
        }

        def build_partner_options() -> str:
            options = []
            for value, label in partner_select_options.items():
                selected = " selected" if partner_filter == value else ""
                options.append(f"<option value='{value}'{selected}>{label}</option>")
            return "".join(options)

        body = f"""
<h1>📊 Stats</h1>

<div class=\"card\">
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

<div class=\"card\" style=\"margin-top:1.2rem;\">
  <h2>Filter</h2>
  <form method=\"get\" action=\"{html.escape(base_path)}\" class=\"filter-form\">
    {hidden_inputs_html}
    <div class=\"row\">
      <label class=\"filter-label\">Min. Samples
        <input type=\"number\" name=\"min_samples\" value=\"{'' if min_samples is None else min_samples}\" min=\"0\">
      </label>
      <label class=\"filter-label\">Min. Ø Viewer
        <input type=\"number\" step=\"0.1\" name=\"min_avg\" value=\"{'' if min_avg is None else f'{min_avg:.1f}'}\" min=\"0\">
      </label>
      <label class=\"filter-label\">Partner Filter
        <select name=\"partner\">{build_partner_options()}</select>
      </label>
      <label class=\"filter-label\">Von Stunde (UTC)
        <input type=\"number\" name=\"hour_from\" value=\"{'' if hour_from is None else hour_from}\" min=\"0\" max=\"23\">
      </label>
      <label class=\"filter-label\">Bis Stunde (UTC)
        <input type=\"number\" name=\"hour_to\" value=\"{'' if hour_to is None else hour_to}\" min=\"0\" max=\"23\">
      </label>
    </div>
    <div class=\"row\" style=\"margin-top:.8rem;\">
      <button class=\"btn\">Anwenden</button>
      <a class=\"btn btn-secondary\" href=\"{html.escape(clear_url)}\">Reset</a>
    </div>
  </form>
  <div class=\"status-meta\" style=\"margin-top:.4rem;\">Hinweis: Stundenangaben beziehen sich auf UTC.</div>
  <div class=\"status-meta\" style=\"margin-top:.8rem;\">Aktive Filter: {' • '.join(filter_descriptions)}</div>
</div>

<div class=\"card\" style=\"margin-top:1.2rem;\">
  <h2>Zeitliche Trends (UTC)</h2>
  {hour_chart_block}
  <div style=\"display:flex; gap:1.2rem; flex-wrap:wrap;\">
    <div style=\"flex:1 1 260px;\">
      <h3>Kategorie gesamt — nach Stunde</h3>
      <table class=\"sortable-table\" data-table=\"category-hour\">
        <thead>
          <tr>
            <th data-sort-type=\"number\">Stunde</th>
            <th data-sort-type=\"number\">Samples</th>
            <th data-sort-type=\"number\">Ø Viewer</th>
            <th data-sort-type=\"number\">Peak Viewer</th>
          </tr>
        </thead>
        <tbody>{category_hour_rows}</tbody>
      </table>
    </div>
    <div style=\"flex:1 1 260px;\">
      <h3>Tracked Streamer — nach Stunde</h3>
      <table class=\"sortable-table\" data-table=\"tracked-hour\">
        <thead>
          <tr>
            <th data-sort-type=\"number\">Stunde</th>
            <th data-sort-type=\"number\">Samples</th>
            <th data-sort-type=\"number\">Ø Viewer</th>
            <th data-sort-type=\"number\">Peak Viewer</th>
          </tr>
        </thead>
        <tbody>{tracked_hour_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<div class=\"card\" style=\"margin-top:1.2rem;\">
  <h2>Tagestrends</h2>
  {weekday_chart_block}
  <div style=\"display:flex; gap:1.2rem; flex-wrap:wrap;\">
    <div style=\"flex:1 1 260px;\">
      <h3>Kategorie gesamt — nach Wochentag</h3>
      <table class=\"sortable-table\" data-table=\"category-weekday\">
        <thead>
          <tr>
            <th data-sort-type=\"number\">Tag</th>
            <th data-sort-type=\"number\">Samples</th>
            <th data-sort-type=\"number\">Ø Viewer</th>
            <th data-sort-type=\"number\">Peak Viewer</th>
          </tr>
        </thead>
        <tbody>{category_weekday_rows}</tbody>
      </table>
    </div>
    <div style=\"flex:1 1 260px;\">
      <h3>Tracked Streamer — nach Wochentag</h3>
      <table class=\"sortable-table\" data-table=\"tracked-weekday\">
        <thead>
          <tr>
            <th data-sort-type=\"number\">Tag</th>
            <th data-sort-type=\"number\">Samples</th>
            <th data-sort-type=\"number\">Ø Viewer</th>
            <th data-sort-type=\"number\">Peak Viewer</th>
          </tr>
        </thead>
        <tbody>{tracked_weekday_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<div class=\"card\" style=\"margin-top:1.2rem;\">
  <div class=\"card-header\">
    <h2>Top Partner Streamer (Tracked)</h2>
    <div class=\"row\" style=\"gap:.6rem; align-items:center;\">
      <div style=\"color:var(--muted); font-size:.9rem;\">Ansicht: {current_view_label}</div>
      <a class=\"btn\" href=\"{html.escape(toggle_href)}\">{toggle_label}</a>
    </div>
  </div>
  <table class=\"sortable-table\" data-table=\"tracked\">
    <thead>
      <tr>
        <th data-sort-type=\"string\">Streamer</th>
        <th data-sort-type=\"number\">Samples</th>
        <th data-sort-type=\"number\">Ø Viewer</th>
        <th data-sort-type=\"number\">Peak Viewer</th>
        <th data-sort-type=\"number\">Partner</th>
      </tr>
    </thead>
    <tbody>{render_table(tracked_items)}</tbody>
  </table>
</div>

<div class=\"card\" style=\"margin-top:1.2rem;\">
  <div class=\"card-header\">
    <h2>Top Deadlock Streamer (Kategorie gesamt)</h2>
    <div style=\"color:var(--muted); font-size:.9rem;\">Ansicht: {current_view_label}</div>
  </div>
  <table class=\"sortable-table\" data-table=\"category\">
    <thead>
      <tr>
        <th data-sort-type=\"string\">Streamer</th>
        <th data-sort-type=\"number\">Samples</th>
        <th data-sort-type=\"number\">Ø Viewer</th>
        <th data-sort-type=\"number\">Peak Viewer</th>
        <th data-sort-type=\"number\">Partner</th>
      </tr>
    </thead>
    <tbody>{render_table(category_items)}</tbody>
  </table>
</div>
{script}
"""

        nav_html = None
        if partner_view:
            nav_html = "<nav class=\"tabs\"><span class=\"tab active\">Stats</span></nav>"

        return web.Response(text=self._html(body, active="stats", nav=nav_html), content_type="text/html")

    async def stats(self, request: web.Request):
        self._require_token(request)
        return await self._render_stats_page(request, partner_view=False)

    async def partner_stats(self, request: web.Request):
        self._require_partner_token(request)
        return await self._render_stats_page(request, partner_view=True)

    def attach(self, app: web.Application):
        app.add_routes([
            web.get("/twitch", self.index),
            web.get("/twitch/add_any", self.add_any),
            web.get("/twitch/add_url", self.add_url),            # kompatibel
            web.get("/twitch/add_login/{login}", self.add_login),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/verify", self.verify),
            web.get("/twitch/stats", self.stats),
            web.get("/twitch/partners", self.partner_stats),
        ])
# --- Lightweight Factory -----------------------------------------------
# Optional: Volle UI nur, wenn alle Callbacks übergeben werden.
def build_app(
    *,
    noauth: bool,
    token: Optional[str],
    partner_token: Optional[str] = None,
    add_cb=None,
    remove_cb=None,
    list_cb=None,
    stats_cb=None,
    verify_cb=None,
) -> web.Application:
    app = web.Application()
    have_full_ui = all(cb is not None for cb in (
        add_cb, remove_cb, list_cb, stats_cb, verify_cb
    ))

    if have_full_ui:
        ui = Dashboard(
            app_token=token,
            noauth=noauth,
            partner_token=partner_token,
            add_cb=add_cb,
            remove_cb=remove_cb,
            list_cb=list_cb,
            stats_cb=stats_cb,
            verify_cb=verify_cb,
        )
        ui.attach(app)
    else:
        # Minimaler Health-Endpoint, damit der Cog-Start nicht crasht
        async def index(request: web.Request):
            return web.Response(text="Twitch dashboard is running.")
        app.add_routes([web.get("/twitch", index)])

    return app

# --- Backwards-Compatibility Shim --------------------------------------
# Erlaubt Aufrufe wie: Dashboard.build_app(...)
try:
    Dashboard.build_app = staticmethod(build_app)  # type: ignore[attr-defined]
except Exception:
    pass
